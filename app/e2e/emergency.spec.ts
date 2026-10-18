import { test, expect, type Page } from '@playwright/test'
import { execFileSync, spawn } from 'node:child_process'
import { mkdtemp, mkdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve, join } from 'node:path'
import { createInterface } from 'node:readline'
import QRCode from 'qrcode-svg'
import { loginApi } from './support/fixtures'

test('аварийная статика: общие компоненты, состояния, профили, публичная инструкция', async ({ browser, page }, testInfo) => {
  test.setTimeout(180_000)
  execFileSync('node', ['node_modules/vite/bin/vite.js', 'build', '--config', 'emergency/vite.config.ts'], { cwd: process.cwd(), stdio: 'pipe' })
  const directory = await mkdtemp(join(tmpdir(), 'emergency-browser-'))
  const shots = process.env.EMERGENCY_SHOTS || testInfo.outputPath('shots')
  await mkdir(shots, { recursive: true })
  const svg = new QRCode({ content: 'hysteria2://fixture-only@203.0.113.2:443/', width: 220, height: 220 }).svg()
  await page.setContent(svg)
  await page.locator('svg').screenshot({ path: join(directory, 'qr.png') })
  const fixture = spawn('python3', [resolve('../infra/tests/emergency_web_fixture.py'), directory], { stdio: ['pipe', 'pipe', 'pipe'] })
  // stderr фикстуры (обрывы TLS от браузера) читаем впустую: полный канал повесит её на выходе.
  fixture.stderr.resume()
  const lines = createInterface({ input: fixture.stdout })[Symbol.asyncIterator]()
  const first = await lines.next()
  const startup = JSON.parse(first.value!) as { port: number, password: string }
  const origin = `https://127.0.0.1:${startup.port}`
  // Origin основной панели из конфига Chromium отвергает на чужом адресе (ERR_INVALID_ARGUMENT).
  const context = await browser.newContext({ ignoreHTTPSErrors: true, extraHTTPHeaders: {}, viewport: { width: 1400, height: 1000 }, colorScheme: 'dark', reducedMotion: 'reduce' })
  const emergency = await context.newPage()
  const errors: string[] = []
  emergency.on('pageerror', e => errors.push(e.message))
  const requests: string[] = []
  emergency.on('request', r => requests.push(r.url()))
  async function action(action: string) {
    fixture.stdin.write(`${JSON.stringify({ action })}\n`)
    return JSON.parse((await lines.next()).value!) as { password: string }
  }
  async function signIn(password: string) {
    await emergency.goto(origin)
    await expect(emergency.locator('input')).toHaveCount(1)
    await emergency.getByLabel('Пароль', { exact: true }).fill(password)
    await emergency.getByRole('button', { name: 'Войти', exact: true }).click()
    await expect(emergency.getByText('Возраст снимка:', { exact: false })).toBeVisible()
  }
  // Сохранённые элементы основной страницы: шрифт, размеры, скругления и отступы должны совпасть.
  const desktopRetained = ['.wrap>header .logo span', '.wrap>header .tabs a.on', '.head h1', '.inp input', '.thead span',
    '.crow', '.crow .who b', '.crow .who small', '.crow .av', '.cdh h2', '.cdh .av', '.dev b', '.ov-broadcast textarea']
  const mobileListRetained = ['.mhead h1', '.mhead p', '.crow', '.crow>.av', '.crow .who b', '.crow .who small']
  const mobileDetailRetained = ['.wrap>header .cbk', '.mhead h1', '.cdh h2', '.cdh .av', '.dev b']
  // Снимок после конца анимаций: смена ширины перестраивает каркас с плавным появлением.
  async function shot(target: Page, name: string) {
    await target.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => undefined))))
    await target.screenshot({ path: join(shots, name), fullPage: true })
  }
  async function metrics(target: Page, selectors: string[]) {
    return target.evaluate(list => Object.fromEntries(list.map((selector) => {
      const element = document.querySelector(selector)
      if (!element) return [selector, 'нет элемента']
      const style = getComputedStyle(element)
      const box = element.getBoundingClientRect()
      // Nuxt дописывает к Manrope метрики запасных шрифтов: сравниваем основной шрифт.
      // Имя клиента в основной панели редактируется (подложка: padding и обратный margin), текст стоит на том же месте.
      const editable = selector.endsWith('.cdh h2')
      return [selector, [style.fontFamily.split(',')[0], style.fontSize, style.fontWeight, style.lineHeight,
        editable ? '' : style.borderRadius, editable ? '' : style.padding,
        selector.includes('cbk') || selector.endsWith('.av') ? `${box.width}x${box.height}` : '']]
    })), selectors)
  }
  try {
    await signIn(startup.password)
    // Ответ пришёл между тиками таймера (часы страницы ушли вперёд, тика не было): свежий снимок не «-1 мин».
    await action('fresh')
    const probe = await context.newPage()
    await probe.clock.install({ time: 1_800_000_000_000 })
    await probe.clock.pauseAt(1_800_000_000_000)
    await probe.route('**/api/state', async (route) => { await probe.clock.setSystemTime(1_800_000_000_900); await route.continue() })
    await probe.goto(origin)
    await expect(probe.getByText('Возраст снимка:', { exact: false })).toHaveText('Возраст снимка: 0 мин')
    await probe.close()
    await shot(emergency, 'emergency-desktop.png')
    const desktopMetrics = await metrics(emergency, desktopRetained)
    // Красный акцент: активная вкладка залита существующим токеном --red.
    expect(await emergency.evaluate(() => [getComputedStyle(document.querySelector('.wrap>header .tabs a.on')!).backgroundColor,
      getComputedStyle(document.documentElement).getPropertyValue('--red').trim()])).toEqual(['rgb(255, 91, 82)', '#ff5b52'])
    await expect(emergency.getByRole('button', { name: 'Новый клиент' })).toHaveCount(0)
    await expect(emergency.getByTestId('device-row')).toHaveCount(2)
    await emergency.getByRole('button', { name: 'Hysteria2', exact: true }).first().click()
    await expect(emergency.locator('.profile code')).toHaveText('hysteria2://fixture-only@203.0.113.2:443/')
    const qr = await emergency.locator('.qr').screenshot()
    expect(qr.length).toBeGreaterThan(100)
    // Загрузку Chromium отменяет на самоподписанном сертификате даже с ignoreHTTPSErrors: файл и имя - запросом с той же сессией.
    const link = emergency.getByRole('link', { name: 'Скачать' })
    await expect(link).toHaveAttribute('download', '')
    const file = await emergency.request.get(new URL((await link.getAttribute('href'))!, origin).href)
    expect([file.status(), file.headers()['content-disposition'], await file.text()])
      .toEqual([200, 'attachment; filename="hysteria2-11.txt"', 'hysteria2://fixture-only@203.0.113.2:443/'])
    await emergency.getByLabel('Сообщение юзерам').fill('Проверка доставки')
    await emergency.getByRole('button', { name: 'Отправить сообщение' }).click()
    await expect(emergency.getByText('Сообщение поставлено в очередь.', { exact: false })).toBeVisible()
    for (const [name, text] of [['partition', 'согласование между экзитами нарушено'], ['recovery', 'До отключения аварийного доступа:'], ['cancel', 'Основное подключение снова недоступно'], ['unknown', 'Статус неизвестен']]) {
      await action(name!)
      await emergency.reload()
      await expect(emergency.getByText(text!, { exact: false }).first()).toBeVisible()
      await shot(emergency, `emergency-${name}.png`)
    }
    const rotated = await action('rotate')
    await signIn(rotated.password)
    await expect(emergency.getByText('Лидер сменился:', { exact: false })).toBeVisible()
    await emergency.setViewportSize({ width: 402, height: 874 })
    await shot(emergency, 'emergency-mobile-list.png')
    const mobileListMetrics = await metrics(emergency, mobileListRetained)
    await emergency.getByTestId('client-row').first().click()
    await expect(emergency.getByTestId('device-row').first()).toBeVisible()
    await shot(emergency, 'emergency-mobile-detail.png')
    const mobileDetailMetrics = await metrics(emergency, mobileDetailRetained)
    expect(await emergency.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await emergency.getByRole('button', { name: 'Назад', exact: true }).click()
    await expect(emergency.getByTestId('client-row').first()).toBeVisible()

    // Render the real main clients page against its API; compare retained styles.
    await loginApi(page)
    const created = await page.request.post('/api/clients', { data: { name: 'e2e-Семён', deviceLimit: 3, filterTraffic: false } })
    expect(created.status()).toBe(200)
    const client = await created.json()
    for (const name of ['телефон', 'планшет']) expect((await page.request.post(`/api/clients/${client.id}/devices`, { data: { name } })).status()).toBe(200)
    await page.addInitScript(() => localStorage.setItem('nuxt-color-mode', 'dark'))
    await page.setViewportSize({ width: 1400, height: 1000 })
    await page.goto(`/clients?id=${client.id}`)
    await expect(page.getByTestId('device-row')).toHaveCount(2)
    await page.evaluate(() => document.fonts.ready)
    await shot(page, 'main-desktop.png')
    expect(await metrics(page, desktopRetained)).toEqual(desktopMetrics)
    await page.setViewportSize({ width: 402, height: 874 })
    await page.goto('/clients')
    await page.getByPlaceholder('Поиск...').fill('e2e-Семён')
    await expect(page.getByTestId('client-row')).toHaveCount(1)
    await shot(page, 'main-mobile-list.png')
    expect(await metrics(page, mobileListRetained)).toEqual(mobileListMetrics)
    await page.getByTestId('client-row').filter({ hasText: 'e2e-Семён' }).click()
    await expect(page.getByTestId('device-row')).toHaveCount(2)
    await shot(page, 'main-mobile-detail.png')
    expect(await metrics(page, mobileDetailRetained)).toEqual(mobileDetailMetrics)
    await page.request.delete(`/api/clients/${client.id}`)

    await action('end')
    await emergency.goto(`${origin}/instructions`)
    await expect(emergency.getByRole('heading', { name: 'Временное подключение Hysteria2' })).toBeVisible()
    await expect(emergency.locator('body')).not.toContainText('e2e-Семён')
    await shot(emergency, 'instructions-mobile.png')
    expect(requests.every(url => url.startsWith(origin))).toBe(true)
    expect(errors).toEqual([])
    console.log('Emergency UI: desktop/mobile, same retained component styles, local assets, role states, profiles and instructions OK')
  }
  finally {
    await context.close()
    fixture.stdin.end()
    await new Promise<void>(done => fixture.once('exit', () => done()))
    await rm(directory, { recursive: true, force: true })
  }
})
