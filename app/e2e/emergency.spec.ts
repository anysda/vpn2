import { test, expect, type Page } from '@playwright/test'
import { execFileSync, spawn } from 'node:child_process'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve, join } from 'node:path'
import { createInterface } from 'node:readline'
import QRCode from 'qrcode-svg'
import { pauseClockAt } from './support/clock'
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
  // Цвета темы на сохранённых элементах: аварийная и основная страница в одной теме совпадают.
  const paletteRetained = ['body', '.wrap>header .logo span', '.head h1', '.thead span', '.crow .who b', '.card.clist']
  async function palette(target: Page) {
    return target.evaluate(list => list.map((selector) => {
      const style = getComputedStyle(document.querySelector(selector)!)
      // Сборка основной панели пишет начало градиента как 0px, аварийная - как 0%: рисуется одинаково.
      return [selector, style.color, style.backgroundColor, style.backgroundImage.replaceAll(' 0px,', ' 0%,'), style.borderTopColor].join(' | ')
    }), paletteRetained)
  }
  // Тема как в основной панели: data-theme уже стоит, когда появляется <body> (до отрисовки,
  // без вспышки другой темы), и следует смене темы системы.
  async function themed(colorScheme: 'light' | 'dark', path = '/') {
    const view = await browser.newPage({ ignoreHTTPSErrors: true, extraHTTPHeaders: {}, viewport: { width: 1400, height: 1000 }, colorScheme, reducedMotion: 'reduce' })
    await view.addInitScript(() => new MutationObserver((_records, observer) => {
      if (!document.body) return
      document.documentElement.dataset.firstTheme = document.documentElement.dataset.theme ?? 'нет'
      observer.disconnect()
    }).observe(document, { childList: true, subtree: true }))
    await view.goto(`${origin}${path}`)
    expect(await view.evaluate(() => [document.documentElement.dataset.firstTheme, document.documentElement.dataset.theme]))
      .toEqual([colorScheme, colorScheme])
    return view
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
    // Первый лидер аварии со сквозным номером поколения 3: лидер не менялся.
    await expect(emergency.getByText('Возраст снимка:', { exact: false })).toBeVisible()
    await expect(emergency.getByText('Лидер сменился', { exact: false })).toHaveCount(0)
    // Ответ пришёл между тиками таймера (часы страницы ушли вперёд, тика не было): свежий снимок не «-1 мин».
    await action('fresh')
    const probe = await context.newPage()
    await pauseClockAt(probe, 1_800_000_000_000)
    await probe.route('**/api/state', async (route) => { await probe.clock.setSystemTime(1_800_000_000_900); await route.continue() })
    await probe.goto(origin)
    await expect(probe.getByText('Возраст снимка:', { exact: false })).toHaveText('Возраст снимка: 0 мин')
    await probe.close()
    await shot(emergency, 'emergency-desktop.png')
    const desktopMetrics = await metrics(emergency, desktopRetained)
    const darkPalette = await palette(emergency)
    // Светлая тема системы: страница входа и панель светлые, акцент - светлый вариант того же --red.
    const light = await themed('light')
    await light.getByLabel('Пароль', { exact: true }).fill(startup.password)
    await light.getByRole('button', { name: 'Войти', exact: true }).click()
    await expect(light.getByText('Возраст снимка:', { exact: false })).toBeVisible()
    await light.evaluate(() => document.fonts.ready)
    expect(await light.evaluate(() => [getComputedStyle(document.querySelector('.wrap>header .tabs a.on')!).backgroundColor,
      getComputedStyle(document.documentElement).getPropertyValue('--red').trim()])).toEqual(['rgb(184, 37, 26)', '#b8251a'])
    const lightPalette = await palette(light)
    expect(lightPalette).not.toEqual(darkPalette)
    await shot(light, 'emergency-light-desktop.png')
    await light.emulateMedia({ colorScheme: 'dark' })
    await expect(light.locator('html')).toHaveAttribute('data-theme', 'dark')
    // Цвет рамки карточки меняется плавным переходом panel.css - ждём его конца.
    await expect.poll(() => palette(light)).toEqual(darkPalette)
    await light.close()
    await (await themed('dark', '/instructions')).close()
    const lightGuide = await themed('light', '/instructions')
    await expect(lightGuide.getByRole('heading', { name: 'Временное подключение Hysteria2' })).toBeVisible()
    await lightGuide.close()
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
    expect(await palette(page)).toEqual(darkPalette)
    // Основная панель «как в системе» в светлой теме - те же цвета, что у аварийной.
    const mainLight = await page.context().newPage()
    await mainLight.emulateMedia({ colorScheme: 'light' })
    await mainLight.addInitScript(() => localStorage.setItem('nuxt-color-mode', 'system'))
    await mainLight.setViewportSize({ width: 1400, height: 1000 })
    await mainLight.goto(`/clients?id=${client.id}`)
    await expect(mainLight.getByTestId('device-row')).toHaveCount(2)
    await mainLight.evaluate(() => document.fonts.ready)
    expect(await palette(mainLight)).toEqual(lightPalette)
    await shot(mainLight, 'main-light-desktop.png')
    await mainLight.close()
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

// Причина недоставки и состояние клиента без доступа. В одном файле с тестом выше:
// оба собирают статику в один каталог, параллельные шарды затёрли бы её друг другу.
test('аварийная панель: причина недоставки по клиенту, «Заблокирован» и «Срок истёк»', async ({ browser }, testInfo) => {
  test.setTimeout(120_000)
  execFileSync('node', ['node_modules/vite/bin/vite.js', 'build', '--config', 'emergency/vite.config.ts'], { cwd: process.cwd(), stdio: 'pipe' })
  const directory = await mkdtemp(join(tmpdir(), 'emergency-delivery-'))
  const shots = process.env.EMERGENCY_SHOTS || testInfo.outputPath('shots')
  await mkdir(shots, { recursive: true })
  await writeFile(join(directory, 'qr.png'), '')
  const fixture = spawn('python3', [resolve('../infra/tests/emergency_web_fixture.py'), directory], { stdio: ['pipe', 'pipe', 'pipe'] })
  fixture.stderr.resume()
  const lines = createInterface({ input: fixture.stdout })[Symbol.asyncIterator]()
  const startup = JSON.parse((await lines.next()).value!) as { port: number, password: string }
  const context = await browser.newContext({ ignoreHTTPSErrors: true, extraHTTPHeaders: {}, viewport: { width: 1400, height: 1000 }, colorScheme: 'dark', reducedMotion: 'reduce' })
  const page = await context.newPage()
  const errors: string[] = []
  page.on('pageerror', (e) => { errors.push(e.message); console.log(`pageerror: ${e.message}`) })
  async function action(name: string) {
    fixture.stdin.write(`${JSON.stringify({ action: name })}\n`)
    await lines.next()
  }
  async function shot(target: Page, name: string) {
    await target.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => undefined))))
    await target.screenshot({ path: join(shots, name), fullPage: true })
  }
  try {
    await action('refused')
    await action('denied')
    await page.goto(`https://127.0.0.1:${startup.port}`)
    await page.getByLabel('Пароль', { exact: true }).fill(startup.password)
    await page.getByRole('button', { name: 'Войти', exact: true }).click()
    await expect(page.getByText('Возраст снимка:', { exact: false })).toBeVisible()
    // Сводка называет клиентов с ошибкой и причину, а не только «Ошибок: N».
    const summary = page.getByTestId('delivery-errors')
    await expect(summary.locator('li')).toHaveText([
      'e2e-Семён — Telegram ответил ошибкой 503, повтор автоматически',
      'e2e-Анна — Не доставлено: чат не найден (Telegram 400), повтора не будет',
    ])
    const anna = page.getByTestId('client-row').filter({ hasText: 'e2e-Анна' })
    await anna.click()
    await expect(page.getByTestId('delivery-error')).toHaveText('Не доставлено: чат не найден (Telegram 400), повтора не будет')
    await shot(page, 'emfix-538-desktop.png')
    // Клиент без доступа: метка состояния вместо точки, как в основной панели.
    for (const [name, text] of [['e2e-Блок', 'Заблокирован'], ['e2e-Истёк', 'Срок истёк']]) {
      const row = page.getByTestId('client-row').filter({ hasText: name! })
      await expect(row.locator('.tag.access-tag')).toHaveText(text!)
      await expect(row.getByTestId('client-online')).toHaveCount(0)
      await row.click()
      await expect(page.getByTestId('client-detail').locator('.cdh .tag.access-tag')).toHaveText(text!)
    }
    await expect(page.getByText('Статус неизвестен')).toHaveCount(0)
    await shot(page, 'emfix-539-desktop.png')
    await page.setViewportSize({ width: 402, height: 874 })
    await expect(page.getByTestId('client-row').filter({ hasText: 'e2e-Истёк' }).locator('.tag.access-tag')).toHaveText('Срок истёк')
    await shot(page, 'emfix-538-mobile-list.png')
    await anna.click()
    await expect(page.getByTestId('delivery-error')).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await shot(page, 'emfix-538-mobile-detail.png')
    expect(errors).toEqual([])
  }
  finally {
    await context.close()
    fixture.stdin.end()
    await new Promise<void>(done => fixture.once('exit', () => done()))
    await rm(directory, { recursive: true, force: true })
  }
})
