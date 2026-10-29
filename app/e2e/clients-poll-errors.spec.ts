import type { Page, Request } from '@playwright/test'
import { expect, hydrated, loginApi, test, uniq } from './support/fixtures'
import { clientCard } from './support/clients'

/**
 * Сбои опроса списка клиентов: зависший GET не замораживает опрос, ошибка API
 * до первой загрузки видна сообщением, а после загрузки не стирает список -
 * ни в одном случае панель не рисует ложные «Пока нет клиентов» и «0 клиентов».
 */

const isList = (r: Request) => r.method() === 'GET' && new URL(r.url()).pathname === '/api/clients'
const LIST = /\/api\/clients$/

async function ownClient(page: Page) {
  const name = uniq('pollerr')
  const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
  expect(r.status(), await r.text()).toBe(200)
  return { id: (await r.json()).id as number, name }
}

const header = (page: Page) => page.locator('.head p')
const loadError = (page: Page) => page.getByRole('alert').filter({ hasText: 'Не удалось загрузить клиентов' })

/**
 * Нет ложной пустоты прямо сейчас: ни «Пока нет клиентов», ни «0 клиентов» в
 * шапке. Снимок, а не ожидание: ожидание дождалось бы следующего тика опроса.
 */
async function noFalseEmpty(page: Page) {
  expect(await page.getByText('Пока нет клиентов').count(), '«Пока нет клиентов» на экране').toBe(0)
  expect(await header(page).textContent(), 'шапка').not.toMatch(/(^|\D)0 клиентов/)
}

test('первый GET списка не отвечает: опрос не замерзает, список появляется за 20 с', async ({ page }) => {
  test.setTimeout(60_000)
  await loginApi(page)
  const { id, name } = await ownClient(page)
  let first = true
  await page.route(url => url.pathname === '/api/clients', async (route) => {
    if (isList(route.request()) && first) {
      first = false
      // Ответа нет вовсе: запрос держится, пока его не бросит сама страница.
      await new Promise(done => setTimeout(done, 40_000))
    }
    await route.continue().catch(() => undefined)
  })
  await page.goto(`/clients?id=${id}`)
  await hydrated(page)
  await expect(clientCard(page, name)).toBeVisible({ timeout: 20_000 })
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.request.delete(`/api/clients/${id}`)
})

test('API отвечает 500 первые 8 с: сообщение об ошибке без ложной пустоты, затем список', async ({ page, guard }) => {
  test.setTimeout(60_000)
  guard.allow(500, LIST, 'GET')
  await loginApi(page)
  const { id, name } = await ownClient(page)
  const until = Date.now() + 8000
  await page.route(url => url.pathname === '/api/clients', route => isList(route.request()) && Date.now() < until
    ? route.fulfill({ status: 500, json: { message: 'сбой списка' } })
    : route.continue())
  await page.goto(`/clients?id=${id}`)
  await hydrated(page)
  await expect(loadError(page)).toBeVisible()
  while (Date.now() < until - 500) {
    await noFalseEmpty(page)
    expect(await loadError(page).isVisible(), 'сообщение об ошибке загрузки').toBe(true)
    await page.waitForTimeout(400)
  }
  await expect(clientCard(page, name)).toBeVisible({ timeout: 10_000 })
  await expect(loadError(page)).toHaveCount(0)
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.request.delete(`/api/clients/${id}`)
})

test('после загрузки один тик опроса отвечает 500: список и счётчик остаются', async ({ page, guard }) => {
  test.setTimeout(60_000)
  guard.allow(500, LIST, 'GET')
  await loginApi(page)
  const { id, name } = await ownClient(page)
  await page.goto(`/clients?id=${id}`)
  await hydrated(page)
  await expect(clientCard(page, name)).toBeVisible()
  const before = await header(page).textContent()
  // Падает один тик целиком: $fetch повторяет GET после 500 сразу, повтор
  // тоже получает 500. Следующий тик через 3 с уже проходит.
  let failFrom = 0
  await page.route(url => url.pathname === '/api/clients', (route) => {
    if (!isList(route.request()) || (failFrom && Date.now() - failFrom > 1000)) return route.continue()
    failFrom ||= Date.now()
    return route.fulfill({ status: 500, json: { message: 'сбой списка' } })
  })
  await page.waitForResponse(r => isList(r.request()) && r.status() === 500, { timeout: 10_000 })
  // Следующий тик через 3 с: всё это время на экране прежний список.
  for (let i = 0; i < 5; i++) {
    await noFalseEmpty(page)
    expect(await clientCard(page, name).isVisible(), 'строка клиента').toBe(true)
    expect(await header(page).textContent(), 'шапка').toBe(before)
    await page.waitForTimeout(400)
  }
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  await page.request.delete(`/api/clients/${id}`)
})

test('положительный контроль: без сбоев список и счётчик, сообщения об ошибке нет', async ({ page }) => {
  await loginApi(page)
  const { id, name } = await ownClient(page)
  await page.goto(`/clients?id=${id}`)
  await hydrated(page)
  await expect(clientCard(page, name)).toBeVisible()
  await expect(async () => {
    const total = await page.getByTestId('client-row').count()
    expect(await header(page).textContent()).toMatch(new RegExp(`^${total} клиент`))
  }).toPass()
  await expect(loadError(page)).toHaveCount(0)
  await page.request.delete(`/api/clients/${id}`)
})
