import type { Page, Request, Route } from '@playwright/test'
import { expect, hydrated, loginApi, test, uniq } from './support/fixtures'
import { clientCard } from './support/clients'

/**
 * Список клиентов опрашивается одним таймером раз в 3 с, сколько бы
 * компонентов страницы ни читали его: запросы не обрывают друг друга, медленный
 * ответ списка всё равно доходит до экрана, а до первого ответа панель не
 * выдаёт «Пока нет клиентов» и «0 клиентов».
 */

const isList = (r: Request) => r.method() === 'GET' && new URL(r.url()).pathname === '/api/clients'

async function ownClient(page: Page) {
  const name = uniq('poll')
  const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
  expect(r.status(), await r.text()).toBe(200)
  return { id: (await r.json()).id as number, name }
}

/** Ответ списка ждёт `ms`; запрос, оборванный самой страницей, не ошибка теста. */
async function slow(route: Route, ms: number) {
  await new Promise(done => setTimeout(done, ms))
  await route.continue().catch(() => undefined)
}

const header = (page: Page) => page.locator('.head p')

test('открытая карточка: не больше 4 GET списка за 9 с и ни одного оборванного', async ({ page }) => {
  await loginApi(page)
  const { id, name } = await ownClient(page)
  const gets: number[] = []
  const aborted: string[] = []
  page.on('request', (r) => { if (isList(r)) gets.push(Date.now()) })
  page.on('requestfailed', (r) => {
    if (isList(r) && r.failure()?.errorText.includes('ERR_ABORTED')) aborted.push(`${Date.now()} ${r.url()}`)
  })
  await page.goto(`/clients?id=${id}`)
  await hydrated(page)
  await expect(clientCard(page, name)).toBeVisible()
  const start = gets[0]!
  await page.waitForTimeout(9000 - (Date.now() - start))
  const inWindow = gets.filter(t => t - start < 9000)
  expect(inWindow.length, `GET /api/clients за 9 с: ${inWindow.map(t => t - start).join(', ')} мс`).toBeLessThanOrEqual(4)
  expect(aborted, 'оборванные GET /api/clients').toEqual([])
  await page.request.delete(`/api/clients/${id}`)
})

test('каждый ответ списка идёт 4 с: список всё равно появляется', async ({ page }) => {
  test.setTimeout(60_000)
  await loginApi(page)
  const { id, name } = await ownClient(page)
  await page.route(url => url.pathname === '/api/clients', route => isList(route.request()) ? slow(route, 4000) : route.continue())
  await page.goto(`/clients?id=${id}`)
  await expect(clientCard(page, name)).toBeVisible({ timeout: 15_000 })
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.request.delete(`/api/clients/${id}`)
})

test('пока список грузится, нет «Пока нет клиентов» и «0 клиентов»', async ({ page }) => {
  await loginApi(page)
  const { id, name } = await ownClient(page)
  let release!: () => void
  const held = new Promise<void>((done) => { release = done })
  // Придержан не только первый ответ: повторные GET до него тоже ждут, иначе
  // пустоту закрыл бы ответ на повтор, а не на первый запрос.
  await page.route(url => url.pathname === '/api/clients', async (route) => {
    if (isList(route.request())) await held
    await route.continue().catch(() => undefined)
  })
  await page.goto('/clients')
  await hydrated(page)
  // Окно дольше такта опроса: ни первый рендер, ни тики до ответа не рисуют пустоту.
  for (let i = 0; i < 8; i++) {
    await expect(page.getByText('Пока нет клиентов')).toHaveCount(0)
    await expect(header(page)).not.toHaveText(/(^|\D)0 клиентов/)
    await page.waitForTimeout(500)
  }
  release()
  await expect(clientCard(page, name)).toBeVisible()
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.request.delete(`/api/clients/${id}`)
})

test('положительный контроль: без задержки список и счётчик, пустой ответ - «Пока нет клиентов»', async ({ page }) => {
  await loginApi(page)
  const { id, name } = await ownClient(page)
  await page.goto(`/clients?id=${id}`)
  await hydrated(page)
  await expect(clientCard(page, name)).toBeVisible()
  // Счётчик шапки - по строкам на экране: список в базе меняют удаления соседних тестов.
  await expect(async () => {
    const total = await page.getByTestId('client-row').count()
    await expect(header(page)).toHaveText(new RegExp(`^${total} клиент`), { timeout: 0 })
  }).toPass()
  await page.request.delete(`/api/clients/${id}`)

  await page.route(url => url.pathname === '/api/clients', route => isList(route.request())
    ? route.fulfill({ json: [] })
    : route.continue())
  await page.goto('/clients')
  await hydrated(page)
  await expect(page.getByText('Пока нет клиентов')).toBeVisible()
  await expect(header(page)).toHaveText(/^0 клиентов · 0 устройств$/)
})
