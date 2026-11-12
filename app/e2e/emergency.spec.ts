import { expect, type Page, type Browser, type BrowserContext, test } from '@playwright/test'
import { execFileSync, spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { mkdtemp, mkdir, rm, writeFile, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve, join, extname } from 'node:path'
import { createInterface } from 'node:readline'
import QRCode from 'qrcode-svg'
import { pauseClockAt } from './support/clock'
import { loginApi } from './support/fixtures'

test.describe('emergency', () => {
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
    const desktopRetained = ['.wrap>header .logo span', '.head h1', '.inp input', '.thead span',
      '.crow', '.crow .who b', '.crow .who small', '.crow .av', '.cdh h2', '.cdh .av', '.dev b', ':is(.ov-broadcast, [data-testid="ov-broadcast"]) textarea']
    const mobileListRetained = ['.mhead h1', '.mhead p', '.crow', '.crow .av', '.crow .who b', '.crow .who small']
    const mobileDetailRetained = ['.wrap>header .cbk', '.mhead h1', '.cdh h2', '.cdh .av', '.dev b']
    // Снимок после конца анимаций: смена ширины перестраивает каркас с плавным появлением.
    async function shot(target: Page, name: string) {
      await target.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => undefined))))
      await target.screenshot({ path: join(shots, name), fullPage: true })
    }
    // Цвета темы на сохранённых элементах: аварийная и основная страница в одной теме совпадают.
    const paletteRetained = ['body', '.wrap>header .logo span', '.head h1', '.thead span', '.crow .who b', '.card.clist']
    async function palette(target: Page) {
      // Отсутствие элемента называет селектор, а не роняет evaluate с TypeError.
      for (const sel of paletteRetained) await expect(target.locator(sel).first(), 'palette: нет элемента ' + sel).toBeVisible()
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
    // «Аварийный режим» в шапке - надпись, а не вкладка: не ссылка и не кнопка, без пилюли,
    // курсор обычный, при наведении ничего не меняется. Шрифт - как у прежней вкладки.
    async function modeLabel(target: Page) {
      const header = target.locator('.wrap>header')
      await expect(header.getByRole('link')).toHaveCount(0)
      await expect(header.getByRole('button', { name: 'Аварийный режим' })).toHaveCount(0)
      const label = header.getByText('Аварийный режим', { exact: true })
      await expect(label).toBeVisible()
      const look = () => label.evaluate((e) => {
        const s = getComputedStyle(e)
        return [s.cursor, s.backgroundColor, s.borderRadius, s.padding, s.boxShadow, s.fontSize, s.fontWeight, s.color]
      })
      const before = await look()
      expect(before.slice(0, 7)).toEqual(['auto', 'rgba(0, 0, 0, 0)', '0px', '0px', 'none', '13px', '500'])
      await label.hover()
      expect(await look()).toEqual(before)
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
      expect(await light.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--red').trim())).toEqual('#b8251a')
      await modeLabel(light)
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
      // Красный акцент - существующий токен --red.
      expect(await emergency.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--red').trim())).toEqual('#ff5b52')
      await modeLabel(emergency)
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
      // Список клиентов - отдельный запрос и может прийти позже карточки: metrics и palette читают его строку.
      await expect(page.getByTestId('client-row').filter({ hasText: 'e2e-Семён' })).toBeVisible()
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
      await expect(mainLight.getByTestId('client-row').filter({ hasText: 'e2e-Семён' })).toBeVisible()
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
      await action('never')
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
      // Ещё не подключался ни одним устройством: короткое «Нет» в строке и в карточке клиента.
      await expect(anna.getByText('Нет', { exact: true })).toBeVisible()
      await expect(page.getByTestId('client-detail').locator('.cdh .tag')).toHaveText('Нет')
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
})

test.describe('emergency-client-name', () => {
  // VPN2-549: имя клиента в заголовке карточки аварийной панели на 390 обрезается
  // многоточием, хотя место есть. Короткое и среднее имя (до 16 знаков) - целиком,
  // длинное - многоточием в одну строку, без горизонтальной прокрутки.
  // Имена подставляются в ответ /api/state: фикстура знает только свои.
  // Статика собирается в свой каталог и отдаётся из него: emergency.spec.ts в
  // соседнем шарде пересобирает общий static/ и затёр бы её посреди прогона.

  const types: Record<string, string> = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.svg': 'image/svg+xml', '.png': 'image/png' }

  let directory: string
  let assets: string
  let fixture: ChildProcessWithoutNullStreams
  let startup: { port: number, password: string }

  test.beforeAll(async () => {
    test.setTimeout(120_000)
    directory = await mkdtemp(join(tmpdir(), 'emergency-name-'))
    assets = join(directory, 'static')
    execFileSync('node', ['node_modules/vite/bin/vite.js', 'build', '--config', 'emergency/vite.config.ts', '--outDir', assets, '--emptyOutDir'], { cwd: process.cwd(), stdio: 'pipe' })
    await writeFile(join(directory, 'qr.png'), '')
    fixture = spawn('python3', [resolve('../infra/tests/emergency_web_fixture.py'), directory], { stdio: ['pipe', 'pipe', 'pipe'] })
    fixture.stderr.resume()
    const lines = createInterface({ input: fixture.stdout })[Symbol.asyncIterator]()
    startup = JSON.parse((await lines.next()).value!) as { port: number, password: string }
  })

  test.afterAll(async () => {
    fixture.stdin.end()
    await new Promise<void>(done => fixture.once('exit', () => done()))
    await rm(directory, { recursive: true, force: true })
  })

  // Клиенты фикстуры: 1 - без Telegram, «Подключён»; 2 - с Telegram, «Подключался».
  async function open(browser: Browser, width: number, names: Record<number, string>): Promise<[BrowserContext, Page]> {
    const origin = `https://127.0.0.1:${startup.port}`
    const context = await browser.newContext({ ignoreHTTPSErrors: true, extraHTTPHeaders: {}, viewport: { width, height: width < 720 ? 844 : 1000 }, colorScheme: 'dark', reducedMotion: 'reduce' })
    await context.route(`${origin}/**`, async (route) => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/state') {
        const response = await route.fetch()
        // До входа ответ 401 без тела: отдаём как есть.
        if (!response.ok()) return route.fulfill({ response })
        const state = await response.json() as { clients: { id: number, name: string }[] }
        for (const client of state.clients) client.name = names[client.id] ?? client.name
        return route.fulfill({ response, json: state })
      }
      if (path.startsWith('/api/')) return route.continue()
      const file = path === '/' ? 'index.html' : path.slice(1)
      return route.fulfill({ body: await readFile(join(assets, file)), contentType: types[extname(file)] ?? 'application/octet-stream' })
    })
    const page = await context.newPage()
    await page.goto(origin)
    await page.getByLabel('Пароль', { exact: true }).fill(startup.password)
    await page.getByRole('button', { name: 'Войти', exact: true }).click()
    await expect(page.getByText('Возраст снимка:', { exact: false })).toBeVisible()
    return [context, page]
  }

  async function heading(page: Page, name: string) {
    await page.getByTestId('client-row').filter({ hasText: name }).click()
    const h2 = page.getByTestId('client-detail').locator('.cdh h2')
    await expect(h2).toHaveText(name)
    await page.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => undefined))))
    return h2.evaluate((e) => {
      const s = getComputedStyle(e)
      const row = e.closest('.cdh')!
      const children = [...row.children].map(c => Math.round(c.getBoundingClientRect().width))
      return {
        text: e.textContent, scrollWidth: e.scrollWidth, clientWidth: e.clientWidth, height: e.getBoundingClientRect().height,
        lineHeight: parseFloat(s.lineHeight) || parseFloat(s.fontSize) * 1.5, textOverflow: s.textOverflow, hyphens: s.hyphens,
        row: Math.round(row.getBoundingClientRect().width), children,
        tag: (row.querySelector('.tag') as HTMLElement | null)?.textContent ?? null,
        pageScroll: document.documentElement.scrollWidth, innerWidth,
      }
    })
  }

  async function shot(page: Page, testInfo: { outputPath: (...p: string[]) => string }, name: string) {
    const shots = process.env.EMERGENCY_SHOTS || testInfo.outputPath('shots')
    await mkdir(shots, { recursive: true })
    await page.screenshot({ path: join(shots, name), fullPage: true })
  }

  test('390: имя 16 знаков рядом со статусом «Подключался» в заголовке карточки целиком', async ({ browser }, testInfo) => {
    const name = 'b17-badclient-16'
    const [context, page] = await open(browser, 390, { 2: name })
    try {
      const m = await heading(page, name)
      await shot(page, testInfo, 'emname-390-16.png')
      expect(m.scrollWidth, `имя обрезано при свободном месте: ${JSON.stringify(m)}`).toBeLessThanOrEqual(m.clientWidth + 1)
    }
    finally {
      await context.unrouteAll({ behavior: 'ignoreErrors' })
      await context.close()
    }
  })

  test('390: короткое имя 8 знаков в заголовке карточки целиком', async ({ browser }, testInfo) => {
    const name = 'b17-shrt'
    const [context, page] = await open(browser, 390, { 2: name })
    try {
      const m = await heading(page, name)
      await shot(page, testInfo, 'emname-390-8.png')
      expect(m.scrollWidth, `имя обрезано: ${JSON.stringify(m)}`).toBeLessThanOrEqual(m.clientWidth + 1)
    }
    finally {
      await context.unrouteAll({ behavior: 'ignoreErrors' })
      await context.close()
    }
  })

  test('390: длинное имя без пробелов - многоточие в одну строку, без горизонтальной прокрутки', async ({ browser }, testInfo) => {
    const name = 'b17-very-long-client-name-without-any-spaces-at-all'
    const [context, page] = await open(browser, 390, { 2: name })
    try {
      const m = await heading(page, name)
      await shot(page, testInfo, 'emname-390-long.png')
      const detail = JSON.stringify(m)
      expect(m.textOverflow, detail).toBe('ellipsis')
      expect(m.scrollWidth, detail).toBeGreaterThan(m.clientWidth)
      expect(m.hyphens, detail).not.toBe('auto')
      expect(m.height, detail).toBeLessThan(m.lineHeight * 1.5)
      expect(m.pageScroll, detail).toBeLessThanOrEqual(m.innerWidth)
    }
    finally {
      await context.unrouteAll({ behavior: 'ignoreErrors' })
      await context.close()
    }
  })

  test('1440: имя 16 знаков в заголовке карточки целиком', async ({ browser }, testInfo) => {
    const name = 'b17-badclient-16'
    const [context, page] = await open(browser, 1440, { 2: name })
    try {
      const m = await heading(page, name)
      await shot(page, testInfo, 'emname-1440-16.png')
      expect(m.scrollWidth, `имя обрезано: ${JSON.stringify(m)}`).toBeLessThanOrEqual(m.clientWidth + 1)
    }
    finally {
      await context.unrouteAll({ behavior: 'ignoreErrors' })
      await context.close()
    }
  })
})
