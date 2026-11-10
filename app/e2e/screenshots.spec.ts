import { request, type APIRequestContext, type Locator, type Page } from '@playwright/test'
import { pauseClockAt } from './support/clock'
import { ADMIN_PASSWORD, ADMIN_USER, authPost, expect, hydrated, loginApi, test } from './support/fixtures'

// Эталонные скриншоты всех страниц панели в трёх размерах окна (проекты desktop,
// narrow, mobile). Расхождение в один пиксель валит тест: рефактор панели обязан
// доказать «не на пиксель». Эталоны снимаются только в CI, в образе
// mcr.microsoft.com/playwright:v<версия>-noble, и лежат в e2e/__screenshots__/<проект>/.
//
// Плавание лечится не допуском, а данными и временем:
//  - клиенты и девайсы заводятся через API с заданными именами (seed), всё
//    чужое из стенда (клиенты, маршруты, правила DNS) предварительно убирается;
//  - часы страницы стоят на FIXED, пояс Europe/Moscow, reduced-motion;
//  - значения, которые сервер выдаёт от времени запроса или от чужих тестов
//    шарда, подменяются в ответе по таблице ниже.
//
// | маршрут                          | поле                                | значение                      | причина |
// | /api/sso                         | enabled, autoRedirect               | false                         | SSO включают чужие тесты шарда |
// | /api/events                      | весь список                         | пять событий, at от FIXED     | журнал пишут и чужие тесты (маршруты, входы), at - время запроса |
// | /api/ops/traffic                 | starts, exits[].bytes, total, sum, prev | узор от FIXED             | вёдра считаются от Date.now() сервера, последнее неполное |
// | /api/ops/activity                | today, days                         | узор от FIXED                 | сутки от Date.now(), дни из трафика чужих тестов |
// | /api/ops/clients-online          | hourly                              | узор                          | часы от Date.now(), трафик чужих тестов |
// | /api/clients/top                 | since, historyStart                 | от FIXED                      | границы периода от Date.now() |
// | /api/clients, /api/clients/<id>  | createdAt (клиент и девайсы)        | по таблице SEED от FIXED      | время создания в БД |
// | /api/ops/adguard                | available, totalToday, blockedToday, blockRatePct, avgMs | 12480, 1860, 14.9, 11.4 | счётчики фейкового AdGuard растут от запросов чужих тестов |
// | /api/clients, /api/clients/<id>  | operation                           | null                          | статус выдачи доступа зависит от того, успела ли нода его подтвердить |
// | /api/dns/log                     | rows[].time                         | FIXED - 37 с * номер строки   | журнал AdGuard пишется от Date.now() |
// | /api/dns/filters                 | black[].updated, white[].updated    | FIXED - 3 ч                   | last_updated = момент старта AdGuard |

const FIXED = Date.UTC(2027, 0, 15, 9, 0, 0)
const MSK_OFFSET_SEC = 3 * 3600

test.use({ timezoneId: 'Europe/Moscow', reducedMotion: 'reduce' })

interface SeedClient { name: string, expiresAt: string | null, deviceLimit: number | null, filterTraffic: boolean, devices: string[], ageDays: number }
const SEED: SeedClient[] = [
  { name: 'Иван Петров', expiresAt: '2030-06-30T00:00:00.000Z', deviceLimit: 3, filterTraffic: true, devices: ['iPhone', 'Ноутбук'], ageDays: 30 },
  { name: 'Мария Сидорова', expiresAt: null, deviceLimit: null, filterTraffic: false, devices: ['Планшет'], ageDays: 20 },
  { name: 'Олег Смирнов', expiresAt: '2030-01-01T00:00:00.000Z', deviceLimit: 5, filterTraffic: false, devices: [], ageDays: 10 },
]
const TEST_ROUTE = /^(\*\.)?e2e-|^198\.51\.100\.|^203\.0\.113\./

const iso = (ms: number) => new Date(ms).toISOString()
const mskDay = (ms: number) => Math.floor((ms / 1000 + MSK_OFFSET_SEC) / 86400)

async function apiContext(): Promise<APIRequestContext> {
  const api = await request.newContext({ baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:31710' })
  const login = await authPost(api, '/sign-in/username', { username: ADMIN_USER, password: ADMIN_PASSWORD })
  expect(login.status(), await login.text()).toBe(200)
  return api
}

async function ok<T = unknown>(r: Awaited<ReturnType<APIRequestContext['get']>>): Promise<T> {
  expect(r.status(), await r.text()).toBeLessThan(300)
  return await r.json() as T
}

/** Стенд в известное состояние: чужие клиенты, маршруты, правила DNS убраны, свои заведены. */
async function seed() {
  const api = await apiContext()
  try {
    const present = await ok<{ id: number }[]>(await api.get('/api/clients'))
    for (const c of present) await api.delete(`/api/clients/${c.id}`)
    await expect.poll(async () => (await ok<unknown[]>(await api.get('/api/clients'))).length, { timeout: 60_000 }).toBe(0)
    const routes = await ok<{ id: number, value: string }[]>(await api.get('/api/routes'))
    for (const r of routes.filter(r => TEST_ROUTE.test(r.value))) await api.delete(`/api/routes/${r.id}`)

    await ok(await api.put('/api/dns/rules', { data: { text: '||mc.yandex.ru^' } }))
    const rewrites = await ok<{ domain: string, answer: string }[] | null>(await api.get('/api/dns/rewrites'))
    for (const w of rewrites ?? []) {
      if (w.domain !== 'test.lan') await api.delete('/api/dns/rewrites', { data: { domain: w.domain, answer: w.answer } })
    }
    await ok(await api.put('/api/dns/services', { data: { ids: ['tiktok'] } }))

    const ids: number[] = []
    for (const c of SEED) {
      const made = await ok<{ id: number }>(await api.post('/api/clients', {
        data: { name: c.name, expiresAt: c.expiresAt, deviceLimit: c.deviceLimit, filterTraffic: c.filterTraffic },
      }))
      ids.push(made.id)
      for (const name of c.devices) await ok(await api.post(`/api/clients/${made.id}/devices`, { data: { name } }))
    }
    // Выдача доступа служит VPN асинхронно: статус «применяется» на экране не должен остаться.
    for (const id of ids) {
      await expect.poll(async () => {
        const d = await ok<{ operation: { status: string } | null }>(await api.get(`/api/clients/${id}`))
        return d.operation?.status ?? 'confirmed'
      }, { timeout: 60_000 }).toBe('confirmed')
    }
  }
  finally {
    await api.dispose()
  }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = Record<string, any>

/** Ответ стенда с исправленными полями, остальное как есть. */
async function pin(page: Page, url: RegExp, patch: (body: never, url: URL) => unknown) {
  await page.route(url, async (route) => {
    const response = await route.fetch()
    if (!response.ok()) return route.fulfill({ response })
    await route.fulfill({ response, json: patch(await response.json() as never, new URL(route.request().url())) })
  })
}

function pinAll(page: Page) {
  const ageOf = (name: string) => FIXED - (SEED.find(c => c.name === name)?.ageDays ?? 1) * 86_400_000
  const traffic = (body: Json, period: string) => {
    const day = period === 'week'
    const n = day ? 7 : 24
    const step = day ? 86400 : 3600
    const cur = day ? mskDay(FIXED) * 86400 - MSK_OFFSET_SEC : Math.floor(FIXED / 1000 / 3600) * 3600
    const exits = (body.exits as { tag: string, bytes: number[] }[]).map((e, k) => ({
      tag: e.tag,
      bytes: Array.from({ length: n }, (_, i) => ((i * 7 + k * 3) % 11 + 1) * (day ? 4e9 : 1.5e8)),
    }))
    const total = Array.from({ length: n }, (_, i) => exits.reduce((s, e) => s + e.bytes[i]!, 0))
    const sum = total.reduce((a, b) => a + b, 0)
    return { ...body, starts: Array.from({ length: n }, (_, i) => cur - (n - 1 - i) * step), exits, total, sum, prev: Math.round(sum * 0.8) }
  }
  return Promise.all([
    pin(page, /\/api\/sso(\?|$)/, b => ({ ...b, enabled: false, autoRedirect: false })),
    pin(page, /\/api\/events(\?|$)/, () => [
      { id: 5, at: FIXED - 4 * 60_000, type: 'node', level: 'ok', title: 'Узел восстановлен', detail: 'Связь с узлом снова есть' },
      { id: 4, at: FIXED - 35 * 60_000, type: 'node', level: 'alarm', title: 'Узел недоступен', detail: 'Нет ответа уже 3 минуты' },
      { id: 3, at: FIXED - 2 * 3_600_000, type: 'client', level: 'info', title: 'Клиент создан', detail: 'Иван Петров' },
      { id: 2, at: FIXED - 5 * 3_600_000, type: 'route', level: 'warn', title: 'Маршрут изменён', detail: 'Правило перенесено на другой выход' },
      { id: 1, at: FIXED - 26 * 3_600_000, type: 'login', level: 'info', title: 'Вход в панель', detail: 'admin' },
    ]),
    pin(page, /\/api\/ops\/traffic(\?|$)/, (b, u) => traffic(b, u.searchParams.get('period') ?? 'day')),
    pin(page, /\/api\/ops\/activity(\?|$)/, () => ({
      today: mskDay(FIXED),
      days: Array.from({ length: 150 }, (_, i) => mskDay(FIXED) - 149 + i)
        .filter(d => (d * 7) % 5 !== 0)
        .map(d => ({ day: d, online: (d * 3) % 4, bytes: ((d * 13) % 9 + 1) * 3e8 })),
    })),
    pin(page, /\/api\/ops\/clients-online(\?|$)/, b => ({ ...b, hourly: Array.from({ length: 24 }, (_, i) => (i * 5) % 4) })),
    pin(page, /\/api\/clients\/top(\?|$)/, (b) => {
      const start = mskDay(FIXED) * 86400 - MSK_OFFSET_SEC
      return { ...b, since: b.period === 'week' ? start - 6 * 86400 : start, historyStart: start - 30 * 86400 }
    }),
    pin(page, /\/api\/clients(\?|$)/, (list: Json[]) => list.map(c => ({ ...c, operation: null, createdAt: iso(ageOf(c.name)) }))),
    pin(page, /\/api\/clients\/\d+(\?|$)/, (c: Json) => ({
      ...c,
      operation: null,
      createdAt: iso(ageOf(c.name)),
      devices: (c.devices as Json[]).map((d, i) => ({ ...d, createdAt: iso(ageOf(c.name) + (i + 1) * 60_000) })),
    })),
    pin(page, /\/api\/ops\/adguard(\?|$)/, b => ({ ...b, available: true, totalToday: 12480, blockedToday: 1860, blockRatePct: 14.9, avgMs: 11.4 })),
    pin(page, /\/api\/dns\/log(\?|$)/, (b: { rows: Json[] }) => ({
      ...b,
      rows: b.rows.map((r, i) => ({ ...r, time: iso(FIXED - i * 37_000) })),
    })),
    pin(page, /\/api\/dns\/filters(\?|$)/, (b: Json) => ({
      ...b,
      black: (b.black as Json[]).map(l => ({ ...l, updated: iso(FIXED - 3 * 3_600_000) })),
      white: (b.white as Json[]).map(l => ({ ...l, updated: iso(FIXED - 3 * 3_600_000) })),
    })),
  ])
}

/** Запросы страницы, которые ещё не закончились (долгие потоки событий не в счёт). */
function trackRequests(page: Page) {
  const pending = new Set<unknown>()
  page.on('request', (r) => { if (r.resourceType() !== 'eventsource') pending.add(r) })
  page.on('requestfinished', r => pending.delete(r))
  page.on('requestfailed', r => pending.delete(r))
  return pending
}

/** Страница готова: шрифты, гидратация, ни одного запроса в полёте 400 мс подряд. */
async function ready(page: Page, pending: Set<unknown>, shown: Locator[]) {
  await hydrated(page)
  for (const l of shown) await expect(l.first()).toBeVisible()
  // Счётчики и кривые Обзора дорисованы: 4 с времени страницы вперёд, таймеры и кадры в порядке очереди.
  await page.clock.runFor(4000)
  await page.evaluate(() => document.fonts.ready)
  let quiet = 0
  await expect.poll(() => {
    quiet = pending.size === 0 ? quiet + 1 : 0
    return quiet
  }, { timeout: 20_000, intervals: [100] }).toBeGreaterThanOrEqual(4)
  // Переходы и конечные анимации CSS доиграны.
  await expect.poll(() => page.evaluate(() => document.getAnimations().filter(a => a.playState === 'running' && Number.isFinite(a.effect?.getComputedTiming().endTime ?? Infinity)).length)).toBe(0)
  await page.evaluate(() => document.fonts.ready)
}

async function shot(page: Page, pending: Set<unknown>, name: string, shown: Locator[] = []) {
  await ready(page, pending, shown)
  await expect.soft(page).toHaveScreenshot(`${name}.png`, { fullPage: true })
}

const DNS_TABS = [
  ['sum', 'Сводка'], ['log', 'Журнал'], ['flt', 'Фильтры'], ['rules', 'Свои правила'],
  ['rw', 'Перезаписи'], ['svc', 'Сервисы'], ['cl', 'Клиенты'], ['set', 'Настройки DNS'],
] as const

test.describe('эталонные скриншоты', () => {
  test.beforeAll(seed)

  test('все страницы панели', async ({ page }, info) => {
    // На телефоне жмут пальцем: click не открывает экран клиента.
    const press = (l: Locator) => info.project.name === 'mobile' ? l.tap() : l.click()
    test.setTimeout(300_000)
    // Часы стоят: ни опросов раз в 30 с, ни перерисовок посреди снимка. Время двигает только runFor.
    await pauseClockAt(page, FIXED)
    await pinAll(page)
    const pending = trackRequests(page)

    await page.goto('/login')
    await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
    await shot(page, pending, 'login')

    await loginApi(page)

    await page.goto('/')
    await expect(page.locator('.ov-nodes')).toBeVisible()
    await expect(page.getByText('загружаю метрики…')).toHaveCount(0)
    await shot(page, pending, 'overview', [
      page.getByText('Вход в панель'), page.getByText('к вчерашнему'), page.getByText('клиента в сети'),
      page.getByText('Трафика пока нет'), page.getByText('14,9%'),
    ])

    await page.goto('/clients')
    await expect(page.getByTestId('client-row')).toHaveCount(SEED.length)
    await shot(page, pending, 'clients')
    const row = page.getByTestId('client-row').filter({ has: page.getByText(SEED[0]!.name, { exact: true }) })
    await press(row)
    const panel = page.getByTestId('client-detail')
    await expect(panel.getByTitle('Имя клиента')).toHaveText(SEED[0]!.name)
    await expect(page.getByTestId('device-row')).toHaveCount(SEED[0]!.devices.length)
    await shot(page, pending, 'client-card')

    await page.goto('/dns')
    await expect(page.getByRole('tab', { name: 'Сводка' })).toBeVisible()
    for (const [id, label] of DNS_TABS) {
      if (id !== 'sum') await press(page.getByRole('tab', { name: label, exact: true }))
      await expect(page.locator('.dns .dpan.on')).toBeVisible()
      await shot(page, pending, `dns-${id}`)
    }

    await page.goto('/routes')
    await expect(page.getByRole('heading', { level: 1, name: 'Маршруты' })).toBeVisible()
    await shot(page, pending, 'routes')

    await page.goto('/settings')
    await expect(page.getByRole('heading', { name: 'Мой профиль' })).toBeVisible()
    await shot(page, pending, 'settings')
  })
})
