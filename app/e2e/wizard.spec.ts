import { expect, test, type Page } from '@playwright/test'
import { execFileSync, spawn } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { createInterface } from 'node:readline'

// Мастер установки против подставного сервера по договору (e2e/support/fake-wizard.mjs):
// все экраны и состояния, назад/вперёд, перезагрузка, вторая вкладка, обрыв связи, устаревшая ссылка.
test('мастер установки: экраны, отказы, ход установки, итог', async ({ browser }, testInfo) => {
  test.setTimeout(240_000)
  execFileSync('node', ['node_modules/vite/bin/vite.js', 'build', '--config', 'wizard/vite.config.ts'], { stdio: 'pipe' })
  const shots = process.env.WIZARD_SHOTS || testInfo.outputPath('shots')
  await mkdir(shots, { recursive: true })
  const fixture = spawn('node', [resolve('e2e/support/fake-wizard.mjs'), resolve('wizard/dist')], { stdio: ['ignore', 'pipe', 'inherit'] })
  const first = await createInterface({ input: fixture.stdout })[Symbol.asyncIterator]().next()
  const { port, token } = JSON.parse(first.value as string) as { port: number, token: string }
  const origin = `http://127.0.0.1:${port}`
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark', reducedMotion: 'reduce', extraHTTPHeaders: {} })
  let page = await context.newPage()

  const set = (body: object) => context.request.post(`${origin}/__set`, { data: body }).then(r => r.json())
  const heading = (p: Page = page) => p.locator('.wz h1')
  async function shot(name: string, p: Page = page) {
    await p.evaluate(() => document.fonts.ready)
    const overflow = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow, `${name}: страница шире окна`).toBeLessThanOrEqual(0)
    await p.screenshot({ path: join(shots, `982ui-${name}.png`), fullPage: true })
  }
  const next = () => page.getByRole('button', { name: 'Далее' }).click()
  const exitForm = () => page.locator('.xadd')

  try {
    // Ссылка без токена и с чужим токеном мастер не открывают.
    await page.goto(origin)
    await expect(page.getByTestId('expired')).toBeVisible()
    await page.goto(`${origin}/#not-the-token`)
    await expect(page.getByTestId('expired')).toBeVisible()
    await shot('link-wrong-token')

    // Токен из фрагмента: обменян на cookie и убран из адресной строки.
    await page.goto(`${origin}/#${token}`)
    await expect(heading()).toHaveText('Экзиты и ключи')
    expect(page.url()).not.toContain(token)
    await expect(page.getByTestId('orchestrator-key')).toContainText('ssh-ed25519')
    await shot('1-empty')

    // Экзит без ключа: причина, «Продолжить без него» - единственный экзит пропущен, дальше нельзя.
    await set({ checks: { 'root@203.0.113.11': ['Вход по ключу не удался: на 203.0.113.11 нет ключа оркестратора в /root/.ssh/authorized_keys'] } })
    await exitForm().getByPlaceholder('root@203.0.113.10').fill('root@203.0.113.11')
    await exitForm().getByPlaceholder('de').fill('nl')
    await exitForm().getByRole('button', { name: 'Проверить', exact: true }).click()
    await expect(exitForm().getByRole('alert')).toContainText('нет ключа оркестратора')
    // Проверенная строка уже в черновике: пока не выбран путь, дальше нельзя.
    await expect(page.getByTestId('exit-row')).toContainText('не годен')
    await shot('1-check-failed')
    await page.getByRole('button', { name: 'Продолжить без него' }).click()
    await expect(page.getByTestId('exit-row')).toHaveCount(1)
    await expect(page.getByTestId('exit-row')).toContainText('без него')
    await page.locator('textarea').fill('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHumanKeyFixture me@laptop')
    await expect(page.getByText('Нужен хотя бы один годный экзит')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Далее' })).toBeDisabled()
    await shot('1-all-skipped')

    // Пропущенный: «Проверить снова» - всё ещё нет ключа; «Заменить адрес» на годный - экзит в деле.
    const nl = page.getByTestId('exit-row').filter({ hasText: 'NL' })
    await nl.getByRole('button', { name: 'Проверить снова' }).click()
    await expect(nl).toContainText('без него')
    await nl.getByRole('button', { name: 'Заменить адрес' }).click()
    await nl.getByLabel('Новый адрес').fill('root@203.0.113.12')
    await nl.getByRole('button', { name: 'Проверить', exact: true }).click()
    await expect(nl).toContainText('годен')
    await expect(nl).toContainText('root@203.0.113.12')

    // Годный экзит, затем тот же тег ещё раз: текст отказа сервера как есть.
    for (const [host, tag] of [['root@203.0.113.10', 'de'], ['root@203.0.113.13', 'de']]) {
      await exitForm().getByPlaceholder('root@203.0.113.10').fill(host!)
      await exitForm().getByPlaceholder('de').fill(tag!)
      await exitForm().getByRole('button', { name: 'Проверить', exact: true }).click()
      if (host === 'root@203.0.113.13') break
      await expect(exitForm().getByText('сервер годен')).toBeVisible()
      await page.getByRole('button', { name: 'Добавить' }).click()
    }
    await expect(exitForm().getByText('Тег «de» уже есть в списке экзитов')).toBeVisible()
    await expect(page.getByTestId('exit-row')).toHaveCount(2)
    await shot('1-duplicate-tag')
    // Третий экзит без ключа остаётся пропущенным до установки.
    await set({ checks: { 'root@203.0.113.14': ['Вход по ключу не удался: на 203.0.113.14 нет ключа оркестратора в /root/.ssh/authorized_keys'] } })
    await exitForm().getByPlaceholder('root@203.0.113.10').fill('root@203.0.113.14')
    await exitForm().getByPlaceholder('de').fill('fi')
    await exitForm().getByRole('button', { name: 'Проверить', exact: true }).click()
    await page.getByRole('button', { name: 'Продолжить без него' }).click()
    await expect(page.getByTestId('exit-row')).toHaveCount(3)
    await shot('1-filled')
    await next()

    // Экран 2 и перезагрузка: тот же экран.
    await expect(heading()).toHaveText('Панель')
    await page.reload()
    await expect(heading()).toHaveText('Панель')
    await expect(page.getByRole('button', { name: 'Далее' })).toBeDisabled()
    const password = page.locator('input[autocomplete="new-password"]')
    await password.fill('correct horse battery staple')
    await expect(password).toHaveAttribute('type', 'password')
    await page.getByRole('button', { name: 'Показать' }).click()
    await expect(password).toHaveAttribute('type', 'text')
    await page.getByRole('button', { name: 'Свой домен' }).click()
    await page.getByPlaceholder('vpn.example.com').fill('vpn.example.com')
    await shot('2-domain-password-shown')
    await next()
    await expect(heading()).toHaveText('Telegram-бот')
    // Назад браузера и обратно; пароль после отправки не показывается.
    await page.goBack()
    await expect(heading()).toHaveText('Панель')
    await expect(password).toHaveValue('')
    await expect(password).toHaveAttribute('placeholder', /задан/)
    await shot('2-password-set')
    await page.goForward()
    await expect(heading()).toHaveText('Telegram-бот')

    // Экран 3: пропуск и включённый бот.
    await shot('3-skipped')
    await page.getByRole('switch', { name: 'Telegram-бот' }).click()
    await expect(page.getByRole('button', { name: 'Далее' })).toBeDisabled()
    await page.getByPlaceholder('123456:ABC…').fill('123456:fixture-token')
    await page.getByPlaceholder('123456789').fill('100200300')
    await shot('3-on')
    await next()

    // Экран 4: ключ один раз, «Далее» - только после отметки.
    await expect(heading()).toHaveText('Бэкапы')
    await expect(page.getByTestId('backup-key')).toContainText('AGE-SECRET-KEY-1')
    const key = await page.getByTestId('backup-key').textContent()
    await expect(page.getByRole('button', { name: 'Далее' })).toBeDisabled()
    await shot('4-key')
    await page.getByRole('button', { name: 'S3' }).click()
    await page.getByPlaceholder('https://s3.eu-central-1.amazonaws.com').fill('https://s3.example.com')
    await page.getByPlaceholder('my-anysda-backups').fill('family-backups')
    await page.getByLabel('Я сохранил ключ').check()
    await expect(page.getByRole('button', { name: 'Далее' })).toBeDisabled()
    await shot('4-s3')
    await page.getByRole('button', { name: 'На этом сервере' }).click()
    // Ключ не в хранилище браузера и не в адресе.
    const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }) + location.href)
    expect(stored).not.toContain(key!.trim())
    await next()
    await expect(heading()).toHaveText('YouTube через РФ')
    await page.getByRole('button', { name: 'Назад' }).click()
    await expect(page.getByText('Ключ уже показан и сохранён вами')).toBeVisible()
    await expect(page.getByTestId('backup-key')).toHaveCount(0)
    await shot('4-seen')
    await next()

    // Экран 5 и итог; «Изменить» ведёт на экран раздела, шаги сверху - обратно.
    await page.getByRole('switch', { name: 'YouTube через РФ' }).click()
    await shot('5-on')
    await next()
    await expect(heading()).toHaveText('Проверьте и установите')
    await page.reload()
    await expect(heading()).toHaveText('Проверьте и установите')
    await expect(page.locator('.wzsum').first()).toContainText('FI · root@203.0.113.14 · без него')
    await shot('6-summary')
    await page.locator('.wzsum').filter({ hasText: 'Telegram-бот' }).getByRole('button', { name: 'Изменить' }).click()
    await expect(heading()).toHaveText('Telegram-бот')
    await page.locator('.wzbar').getByRole('button', { name: /Проверка/ }).click()
    await expect(heading()).toHaveText('Проверьте и установите')
    await page.getByRole('button', { name: 'Установить' }).click()

    // Экран 7: ход, длинный журнал идёт за хвостом, пока его не прокрутили вверх.
    await expect(heading()).toHaveText('Установка')
    const stage = (name: string, state: string, failed: string[] = []) => ({ name, state, nodes: ['ru', 'de', 'nl'], failed })
    const done = ['preflight', '00-bootstrap', '05-mgmt-mesh', '10-foreign', '28-wireguard'].map(n => stage(n, 'done'))
    const log = (n: number) => Array.from({ length: n }, (_, i) => `TASK [base : строка ${i + 1}] ok: [de]`).join('\n')
    const job = { id: 1, kind: 'install', tag: '', host: '', state: 'running', log: log(200), error: '', actions: [], skipped: [], result: null }
    await set({ job: { ...job, stages: [...done, stage('29-openvpn', 'running'), stage('30-frontend', 'pending'), stage('99-verify', 'pending')] } })
    const pre = page.locator('.xlog pre')
    await expect(pre).toContainText('строка 200')
    const atBottom = () => pre.evaluate(e => e.scrollHeight - e.scrollTop - e.clientHeight < 8)
    await expect.poll(atBottom).toBe(true)
    await shot('7-running')
    await pre.evaluate((e) => {
      e.scrollTop = 0
      e.dispatchEvent(new Event('scroll'))
    })
    await set({ job: { ...job, log: log(260), stages: [...done, stage('29-openvpn', 'running')] } })
    await expect(pre).toContainText('строка 260')
    expect(await pre.evaluate(e => e.scrollTop)).toBe(0)

    // Вторая вкладка: то же состояние, шаги черновика закрыты.
    const second = await context.newPage()
    await second.goto(origin)
    await expect(heading(second)).toHaveText('Установка')
    await expect(second.locator('.wzbar button:not([disabled])')).toHaveCount(0)
    await second.close()

    // Стадия упала: «Повторить» и «Продолжить без NL» доводят до конца.
    const failed = { ...job, state: 'failed', error: 'Стадия 10-foreign упала на nl: недоступен', actions: ['retry', 'skip', 'forget'], log: log(30) }
    await set({ job: { ...failed, stages: [...done.slice(0, 3), stage('10-foreign', 'failed', ['nl'])] } })
    await expect(page.getByRole('button', { name: 'Продолжить без NL' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Повторить' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Забыть' })).toHaveCount(0)
    await shot('7-stage-failed')
    await page.getByRole('button', { name: 'Продолжить без NL' }).click()
    expect((await set({})).state.job.skipped).toEqual(['nl'])
    await set({ job: { ...failed, stages: [...done, stage('30-frontend', 'failed', ['ru'])], error: 'Стадия 30-frontend упала на ru' } })
    await page.getByRole('button', { name: 'Повторить' }).click()
    expect((await set({})).state.job.state).toBe('running')

    // Обрыв связи (перезапуск Caddy): тихо до 10 с, потом «связь восстанавливается», затем тот же экран.
    await set({ job: { ...job, stages: [...done, stage('30-frontend', 'running')] }, offline: 18_000 })
    await page.waitForTimeout(6000)
    await expect(page.getByText('связь восстанавливается')).toHaveCount(0)
    await expect(page.getByText('связь восстанавливается')).toBeVisible({ timeout: 10_000 })
    await shot('7-connection-lost')
    await expect(page.getByText('связь восстанавливается')).toHaveCount(0, { timeout: 15_000 })
    await expect(heading()).toHaveText('Установка')

    // Перезагрузка entry посреди установки: задание оборвалось, только «Повторить».
    await set({ job: { ...job, state: 'interrupted', actions: ['retry', 'forget'], stages: [...done, stage('30-frontend', 'failed')] } })
    await page.reload()
    await expect(page.locator('.xjob .tag')).toHaveText('Оборвалось')
    await expect(page.getByRole('button', { name: 'Забыть' })).toHaveCount(0)
    await shot('7-interrupted')

    // Установка кончилась при закрытой вкладке: ссылка открывает итог; предупреждение - не ошибка.
    await page.close()
    const systems = [
      { name: 'Экзит DE', state: 'ok', detail: 'работает' }, { name: 'Экзит NL', state: 'ok', detail: 'работает' },
      { name: 'Экзит FI', state: 'warning', detail: 'пропущен при установке - добавьте в настройках панели' },
      { name: 'VLESS, Hysteria2, WireGuard, OpenVPN, IKEv2', state: 'ok', detail: 'работают' },
      { name: 'Панель', state: 'warning', detail: 'сертификат самоподписанный, отпечаток SHA-256 AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89' },
      { name: 'Telegram-бот', state: 'ok', detail: 'на связи' }, { name: 'Бэкапы', state: 'ok', detail: 'каждую ночь на этом сервере' },
      { name: 'Мониторинг', state: 'ok', detail: 'все узлы на связи' },
    ]
    await set({ phase: 'done', job: { ...job, state: 'done', stages: [...done, stage('99-verify', 'done')] }, status: { panel_url: 'https://vpn.example.com/', systems } })
    page = await context.newPage()
    await page.goto(origin)
    await expect(heading()).toHaveText('Готово')
    await expect(page.getByTestId('system')).toHaveCount(systems.length)
    await expect(page.locator('code.wzbad')).toHaveCount(0)
    await expect(page.locator('code.wzwarn')).toHaveCount(2)
    await expect(page.getByRole('link', { name: 'Открыть панель' })).toHaveAttribute('href', 'https://vpn.example.com/')
    await shot('8-done-warnings')
    await set({ status: { panel_url: 'https://203.0.113.1/', systems: [...systems.slice(0, 6), { name: 'Бэкапы', state: 'failed', detail: 'S3 не принял ключи: проверьте access key' }, systems[7]] } })
    await page.reload()
    await expect(page.locator('code.wzbad')).toHaveCount(1)
    await shot('8-done-failure')

    // Телефон: ничего не шире экрана.
    await page.setViewportSize({ width: 390, height: 844 })
    await shot('8-phone')
    await set({ phase: 'installing', job: { ...failed, stages: [...done, stage('10-foreign', 'failed', ['nl'])] } })
    await page.reload()
    await expect(page.getByRole('button', { name: 'Повторить' })).toBeVisible()
    await shot('7-phone')
    await set({ phase: 'filling', job: null, draft: { step: 1 } })
    await page.reload()
    await expect(page.getByTestId('exit-row')).toHaveCount(3)
    await shot('1-phone')

    // Сессия устарела посреди заполнения: ясный экран, черновик на сервере цел.
    await page.setViewportSize({ width: 1440, height: 900 })
    await set({ expired: true })
    await page.reload()
    await expect(page.getByTestId('expired')).toBeVisible()
    await shot('link-expired')
    expect((await set({})).state.draft.exits).toHaveLength(3)
  }
  finally {
    fixture.kill()
    await context.close()
  }
})
