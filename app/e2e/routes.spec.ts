import type { Page, Request } from '@playwright/test'
import { expect, hydrated, loginApi, test, toast } from './support/fixtures'

// Тестовые значения: домены e2e-*, адреса из TEST-NET (RFC 5737) — их снесёт
// teardown, а на стенде они никуда не ведут.
const stamp = () => `${Date.now().toString(36)}${Math.floor(Math.random() * 1e4).toString(36)}`
const PLACEHOLDER = 'netflix.com / *.openai.com / 8.8.8.8/32'

// Ячейка доски называется по стране экзита: «Нидерланды», «Финляндия · WARP».
const regions = new Intl.DisplayNames(['ru'], { type: 'region' })
const exitName = (tag: string, warp = false) => `${regions.of(tag.toUpperCase())}${warp ? ' · WARP' : ''}`
const exact = (text: string) => new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`)

function routesForm(page: Page) {
  return page.locator('form').filter({ has: page.getByPlaceholder(PLACEHOLDER) })
}

function chip(page: Page, value: string) {
  return page.locator('.rc[draggable="true"]').filter({ hasText: exact(value) })
}

function cellOf(page: Page, value: string) {
  return page.locator('.cell').filter({ has: chip(page, value) })
}

/** Ячейка доски по имени, как в выборе выхода: «Германия» или «Германия · WARP». */
function cellNamed(page: Page, name: string) {
  const [country, warp] = name.split(' · ')
  return page.locator('.ex').filter({ has: page.locator('.eh b', { hasText: exact(country!) }) })
    .locator('.cell').filter({ has: page.locator('.cl span', { hasText: exact(warp ? 'WARP' : 'Напрямую') }) })
}

/** Счётчик POST /api/routes — ровно один запрос на одно добавление. */
function countPosts(page: Page) {
  const posts: Request[] = []
  page.on('request', (r) => {
    if (r.method() === 'POST' && new URL(r.url()).pathname === '/api/routes') posts.push(r)
  })
  return posts
}

async function openRoutes(page: Page) {
  await loginApi(page)
  await page.goto('/routes')
  await hydrated(page)
  // Выбор выхода появляется, когда пришёл список выходов sing-box.
  await expect(routesForm(page).getByRole('combobox')).toContainText(exitName('ru'))
}

async function pickExit(page: Page, name: string) {
  await routesForm(page).getByRole('combobox').click()
  await page.getByRole('option', { name: exact(name) }).click()
}

async function addRule(page: Page, value: string, exit: string) {
  await pickExit(page, exit)
  const input = routesForm(page).getByPlaceholder(PLACEHOLDER)
  await input.fill(value)
  const resp = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/routes')
  await routesForm(page).getByRole('button', { name: 'Добавить' }).click()
  expect((await resp).status()).toBe(200)
  await expect(toast(page, 'Правило добавлено')).toBeVisible()
  await expect(input).toHaveValue('')
  await expect(chip(page, value)).toBeVisible()
}

test.describe('маршрутизация', () => {
  test('домен, IP, подсеть и *.домен на разные выходы, перенос и удаление', async ({ page }) => {
    const posts = countPosts(page)
    await openRoutes(page)
    const s = stamp()
    const cases = [
      { value: `e2e-${s}.example.com`, exit: exitName('nl') },
      { value: `198.51.100.${10 + Math.floor(Math.random() * 200)}`, exit: exitName('fi') },
      { value: '203.0.113.0/24', exit: exitName('us', true) },
      { value: `*.e2e-${s}.example.org`, exit: exitName('ru') },
    ]
    for (const c of cases) {
      await addRule(page, c.value, c.exit)
      await expect(cellNamed(page, c.exit).filter({ has: chip(page, c.value) })).toHaveCount(1)
    }
    expect(posts, 'одно добавление = один POST').toHaveLength(cases.length)
    await expect(page.getByText(/уже существует|Ошибка/)).toHaveCount(0)

    const stored: { value: string, type: string, outbound: string }[] = await (await page.request.get('/api/routes')).json()
    const byValue = new Map(stored.map(r => [r.value, r]))
    expect(byValue.get(cases[0]!.value)).toMatchObject({ type: 'domain', outbound: 'hy2-nl-direct' })
    expect(byValue.get(cases[1]!.value)).toMatchObject({ type: 'ip_cidr', outbound: 'hy2-fi-direct' })
    expect(byValue.get(cases[2]!.value)).toMatchObject({ type: 'ip_cidr', outbound: 'hy2-us-warp' })
    expect(byValue.get(cases[3]!.value)).toMatchObject({ type: 'domain', outbound: 'direct-ru' })

    // Изменение: перенос правила на другой выход перетаскиванием (PATCH).
    const moved = cases[0]!.value
    const target = cellNamed(page, exitName('fi', true))
    const patched = page.waitForResponse(r => r.request().method() === 'PATCH' && /\/api\/routes\/\d+$/.test(r.url()))
    await chip(page, moved).dragTo(target)
    expect((await patched).status()).toBe(200)
    await expect(toast(page, `«${moved}» → ${exitName('fi', true)}`)).toBeVisible()
    await expect(target.filter({ has: chip(page, moved) })).toHaveCount(1)
    const after: { value: string, outbound: string }[] = await (await page.request.get('/api/routes')).json()
    expect(after.find(r => r.value === moved)?.outbound).toBe('hy2-fi-warp')

    // Удаление: крестик на чипе виден при наведении.
    for (const c of cases) {
      const ch = chip(page, c.value)
      await ch.hover()
      await ch.getByRole('button').click()
      await expect(toast(page, `«${c.value}» удалено`)).toBeVisible()
      await expect(ch).toHaveCount(0)
    }
    const left: { value: string }[] = await (await page.request.get('/api/routes')).json()
    expect(left.filter(r => cases.some(c => c.value === r.value))).toEqual([])
  })

  // Перенос виден сразу, до ответа сервера; ошибка PATCH возвращает правило
  // в прежнюю ячейку, а в базе остаётся старый выход.
  test('перетаскивание переносит сразу и откатывается при ошибке API', async ({ page, guard }) => {
    guard.allow(500, /\/api\/routes\/\d+$/, 'PATCH')
    await openRoutes(page)
    const value = `e2e-drag-${stamp()}.example.com`
    await addRule(page, value, exitName('nl'))
    const from = cellNamed(page, exitName('nl'))
    const to = cellNamed(page, exitName('fi', true))

    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => { release = resolve })
    await page.route(/\/api\/routes\/\d+$/, async (route) => {
      if (route.request().method() !== 'PATCH') return route.fallback()
      await held
      await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ statusCode: 500, statusMessage: 'boom' }) })
    })
    const patched = page.waitForResponse(r => r.request().method() === 'PATCH' && /\/api\/routes\/\d+$/.test(r.url()))
    await chip(page, value).dragTo(to)
    // Ответа ещё нет, а правило уже в новой ячейке.
    await expect(to.filter({ has: chip(page, value) })).toHaveCount(1)
    await expect(from.filter({ has: chip(page, value) })).toHaveCount(0)
    release()
    expect((await patched).status()).toBe(500)
    await expect(toast(page, 'Ошибка перемещения')).toBeVisible()
    await expect(from.filter({ has: chip(page, value) })).toHaveCount(1)
    await expect(to.filter({ has: chip(page, value) })).toHaveCount(0)
    await page.unroute(/\/api\/routes\/\d+$/)
    const stored: { value: string, outbound: string }[] = await (await page.request.get('/api/routes')).json()
    expect(stored.find(r => r.value === value)?.outbound).toBe('hy2-nl-direct')
  })

  test('проверка адреса показывает цепочку и подсвечивает своё правило', async ({ page, guard }) => {
    guard.allow(503, /\/api\/routes\/check/, 'GET')
    await openRoutes(page)
    const value = `e2e-check-${stamp()}.example.com`
    await addRule(page, value, exitName('nl'))
    const stored: { id: number, value: string }[] = await (await page.request.get('/api/routes')).json()
    const id = stored.find(r => r.value === value)!.id

    // Ответ на «slow» держим, пока не придёт следующий ввод: запоздавший
    // ответ не должен перетереть свежий.
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => { release = resolve })
    const answers: Record<string, { status: number, body: object }> = {
      [value]: { status: 200, body: { query: value, ip: null, reason: 'manual', rule: value, routeId: id, outbound: 'hy2-nl-direct', delay: 41 } },
      'slow.example.com': { status: 200, body: { query: 'slow.example.com', ip: null, reason: 'ru', rule: '.ru', routeId: null, outbound: 'direct-ru', delay: 4 } },
      'other.example.com': { status: 200, body: { query: 'other.example.com', ip: '192.0.2.1', reason: 'default', rule: '', routeId: null, outbound: 'hy2-fi-direct', delay: null } },
      'down.example.com': { status: 503, body: { statusCode: 503, statusMessage: 'route_plan_unavailable', message: 'Роутер ещё не выложил маршруты' } },
      // Пустое поле - разбор примера, его же спрашивает новое правило на доске.
      'chatgpt.com': { status: 200, body: { query: 'chatgpt.com', ip: null, reason: 'default', rule: '', routeId: null, outbound: 'hy2-us-direct', delay: 52 } },
    }
    await page.route(/\/api\/routes\/check\?/, async (route) => {
      const q = new URL(route.request().url()).searchParams.get('q')!
      if (q === 'slow.example.com') await held
      const a = answers[q]!
      await route.fulfill({ status: a.status, contentType: 'application/json', body: JSON.stringify(a.body) })
    })
    const input = page.getByRole('textbox', { name: 'Проверить адрес' })
    const chain = page.getByTestId('route-check')

    await input.fill(value)
    await expect(chain.locator('.ch')).toHaveText([value, `правило ${value}`, `${exitName('nl')}41 мс`])
    await expect(chain.locator('.why')).toHaveText('своё правило')
    await expect(chip(page, value)).toHaveClass(/\bhit\b/)
    await expect(page.locator('.rc.hit')).toHaveCount(1)

    await input.fill('slow.example.com')
    await page.waitForRequest(r => r.url().includes('q=slow.example.com'))
    await input.fill('other.example.com')
    await expect(chain.locator('.ch')).toHaveText(['other.example.com', 'по умолчанию', `${exitName('fi')}— мс`])
    await expect(chain.locator('.ch').nth(2)).toHaveClass(/\bbad\b/)
    await expect(chain.locator('.why')).toHaveText('ни одно правило не подошло')
    await expect(page.locator('.rc.hit')).toHaveCount(0)
    release()
    await page.waitForResponse(r => r.url().includes('q=slow.example.com'))
    await expect(chain.locator('.ch').first()).toHaveText('other.example.com')

    await input.fill('down.example.com')
    await expect(chain).toHaveText('Роутер ещё не выложил маршруты')
    // Поле очищено: вместо ошибки снова серый разбор примера.
    await input.fill('')
    await expect(chain.locator('.ch')).toHaveText(['chatgpt.com', 'по умолчанию', `${exitName('us')}52 мс`])
    await expect(chain).toHaveClass(/\bdemo\b/)
  })

  test('проверка адреса без выбора клиента, с подсказкой chatgpt.com', async ({ page }) => {
    const asked: Array<string | null> = []
    await page.route(/\/api\/routes\/check\?/, async (route) => {
      asked.push(new URL(route.request().url()).searchParams.get('client'))
      const body = { query: 'chatgpt.com', ip: null, reason: 'default', rule: '', routeId: null, outbound: 'hy2-us-direct', delay: 52 }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    })
    await openRoutes(page)
    const input = page.getByRole('textbox', { name: 'Проверить адрес' })
    await expect(input).toHaveAttribute('placeholder', 'chatgpt.com')
    await expect(page.locator('.chk').getByRole('combobox')).toHaveCount(0)

    // До ввода под полем разбор примера, серым.
    const chain = page.getByTestId('route-check')
    await expect(chain.locator('.ch')).toHaveText(['chatgpt.com', 'по умолчанию', `${exitName('us')}52 мс`])
    await expect(chain).toHaveClass(/\bdemo\b/)
    await expect(input).toHaveValue('')

    // Введённый адрес - уже не пример: ответ обычным цветом.
    await input.fill('chatgpt.com')
    await expect.poll(() => asked.length).toBeGreaterThan(1)
    await expect(chain).not.toHaveClass(/\bdemo\b/)
    await expect(chain.locator('.ch')).toHaveText(['chatgpt.com', 'по умолчанию', `${exitName('us')}52 мс`])
    expect(asked.every(c => c === null), 'клиент в проверку не передаётся').toBe(true)
  })

  test('ячейка прокручивается после четырёх правил и не меняет высоту', async ({ page }) => {
    await openRoutes(page)
    const exit = exitName('fi')
    const cell = cellNamed(page, exit)
    const list = cell.locator('.rcs')
    const have = await cell.locator('.rc').count()
    // Высота ячеек и доски одна при любом числе правил: как при четырёх.
    const sizes = () => page.locator('.board').evaluate(b => [b.getBoundingClientRect().height,
      ...[...b.querySelectorAll('.cell')].map(c => c.getBoundingClientRect().height)])
    const before = await sizes()
    const height = (el: Element) => el.getBoundingClientRect().height
    expect(await cellNamed(page, exitName('fi', true)).evaluate(height)).toBe(await cell.evaluate(height))
    const s = stamp()
    for (let i = 0; have + i < 5 || i < 1; i++) {
      await addRule(page, `e2e-scroll-${i}-${s}.example.com`, exit)
      if (have + i + 1 === 4) {
        // Четыре правила видны целиком, без прокрутки.
        await expect(list).not.toHaveClass(/\bscroll\b/)
        expect(await list.evaluate(el => el.scrollHeight - el.clientHeight)).toBeLessThanOrEqual(0)
      }
    }
    await expect(list).toHaveClass(/\bscroll\b/)
    const box = await list.evaluate(el => ({ scroll: el.scrollHeight, client: el.clientHeight, top: el.scrollTop }))
    expect(box.scroll).toBeGreaterThan(box.client)
    // Последнее добавленное прокручено в вид.
    expect(box.top + box.client).toBeGreaterThanOrEqual(box.scroll - 1)
    await expect(cellOf(page, `e2e-scroll-0-${s}.example.com`)).toHaveCount(1)
    expect(await sizes()).toEqual(before)
  })

  // VPN2-55: «правило появилось в списке, и тут же ошибка». Форма уходит по
  // Enter и по кнопке, а флаг загрузки не мешает второй отправке, пока первая
  // в полёте: второй POST получает 409 «уже существует» (или 500, если оба
  // прошли проверку на дубль раньше вставки).
  for (const how of ['двойной Enter', 'двойной клик по «Добавить»'] as const) {
    test(`${how} добавляет правило один раз и без ошибки`, async ({ page }) => {
      const posts = countPosts(page)
      await openRoutes(page)
      for (let i = 0; i < 5; i++) {
        const value = `e2e-dbl-${stamp()}.example.net`
        const input = routesForm(page).getByPlaceholder(PLACEHOLDER)
        await input.fill(value)
        const before = posts.length
        if (how === 'двойной Enter') {
          // Два submit подряд в одном такте — как второй Enter или автоповтор клавиши.
          await routesForm(page).evaluate((f: HTMLFormElement) => { f.requestSubmit(); f.requestSubmit() })
        }
        else {
          await routesForm(page).getByRole('button', { name: 'Добавить' }).dblclick()
        }
        await expect(chip(page, value)).toBeVisible()
        await expect(input).toHaveValue('')
        await page.waitForLoadState('networkidle')
        expect(posts.length - before, `попытка ${i + 1}: POST на одно правило`).toBe(1)
        await expect(page.getByText(/уже существует/)).toHaveCount(0)
      }
    })
  }

  test('кривое значение отклоняется понятной ошибкой', async ({ page, guard }) => {
    guard.allow(400, /\/api\/routes$/, 'POST')
    await openRoutes(page)
    const input = routesForm(page).getByPlaceholder(PLACEHOLDER)
    await input.fill('not a domain')
    await routesForm(page).getByRole('button', { name: 'Добавить' }).click()
    await expect(toast(page, 'Нужен домен или IPv4/CIDR').getByText('Нужен домен или IPv4/CIDR', { exact: true })).toBeVisible()
    await expect(input).toHaveValue('not a domain')
  })

  // VPN2-69: битое тело h3 отвечал английским «Invalid JSON body», и бот
  // пересылал его в Telegram как есть.
  test('битый JSON в запросе — ответ по-русски с кодом в statusMessage', async ({ page }) => {
    await openRoutes(page)
    const r = await page.request.post('/api/routes', { headers: { 'content-type': 'application/json' }, data: Buffer.from('{"value": ') })
    expect(r.status()).toBe(400)
    expect(await r.json()).toMatchObject({ statusMessage: 'invalid_json', message: 'Запрос не в формате JSON' })
  })

  // VPN2-67: русский текст ошибки шёл в statusMessage, а h3 и Caddy оставляют
  // от него в строке статуса латиницу или «Conflict». Тост обязан показать
  // текст сервера дословно.
  test('дубль правила показывает русскую ошибку сервера дословно', async ({ page, guard }) => {
    guard.allow(409, /\/api\/routes$/, 'POST')
    await openRoutes(page)
    const value = `e2e-dup-${stamp()}.example.net`
    await addRule(page, value, exitName('nl'))
    const input = routesForm(page).getByPlaceholder(PLACEHOLDER)
    await input.fill(value)
    const resp = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/routes')
    await routesForm(page).getByRole('button', { name: 'Добавить' }).click()
    expect((await resp).status()).toBe(409)
    const text = `Правило «${value}» уже существует`
    await expect(toast(page, text)).toBeVisible()
    await expect(toast(page, text).getByText(text, { exact: true })).toBeVisible()
  })
})
