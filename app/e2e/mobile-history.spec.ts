import type { Locator, Page } from '@playwright/test'
import { expect, hydrated, loginApi, test, toast, uniq } from './support/fixtures'
import { clientCard, clientName, openHome } from './support/clients'
import { waitAccess } from './support/access'

// Телефон: экраны поверх раздела (карточка клиента, нижние шторки) живут в
// истории браузера. Системный «назад» (Android, жест iOS) закрывает верхний
// слой, а не уводит со страницы; закрытие кнопкой не оставляет пустых шагов.
test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

const panel = (page: Page) => page.getByTestId('client-detail')
const path = (page: Page) => { const u = new URL(page.url()); return u.pathname + u.search }

async function newClient(page: Page) {
  await loginApi(page)
  const name = uniq('hist')
  const res = await page.request.post('/api/clients', { data: { name } })
  expect(res.status()).toBe(200)
  return { name, id: (await res.json() as { id: number }).id }
}

const step = (page: Page) => page.evaluate(() => Number(history.state?.position))

/** Открыть шторку: она видна и легла в историю шагом сверху (успела, как успевает палец). */
async function openSheet(page: Page, sheet: Locator, tap: () => Promise<void>) {
  const at = await step(page)
  await tap()
  await expect(sheet).toBeVisible()
  await expect.poll(() => step(page), { message: 'шторка — шаг истории' }).toBe(at + 1)
}

/** С Обзора в Клиенты таб-баром: у раздела есть куда уходить «назад». */
async function clientsFromOverview(page: Page, name: string) {
  await openHome(page, '/')
  await page.locator('.mbar').getByRole('link', { name: 'Клиенты' }).tap()
  await expect(page).toHaveURL(/\/clients$/)
  await page.getByPlaceholder('Поиск...').fill(name)
  await expect(clientCard(page, name)).toHaveCount(1)
}

test('«назад» с карточки клиента возвращает к списку Клиентов', async ({ page }) => {
  const c = await newClient(page)
  await clientsFromOverview(page, c.name)
  await clientCard(page, c.name).tap()
  await expect(clientName(panel(page))).toHaveText(c.name)
  expect(path(page)).toBe(`/clients?id=${c.id}`)

  await page.goBack()
  expect(path(page)).toBe('/clients')
  await expect(clientCard(page, c.name)).toBeVisible()
  await expect(panel(page)).toBeHidden()
  await expect(page.getByRole('button', { name: 'Назад' })).toHaveCount(0)

  await page.goForward()
  await expect(clientName(panel(page))).toHaveText(c.name)
  await expect(clientCard(page, c.name)).toBeHidden()

  // Обновление страницы оставляет на карточке; «Назад» в шапке — к списку, без лишнего шага.
  await page.reload()
  await expect(clientName(panel(page))).toHaveText(c.name)
  await page.getByRole('button', { name: 'Назад' }).tap()
  await expect(clientCard(page, c.name)).toBeVisible()
  expect(path(page)).toBe('/clients')
  await page.goBack()
  await expect(page).toHaveURL(/\/$/)
})

test('прямая ссылка на клиента открывает карточку, «Назад» ведёт к списку', async ({ page }) => {
  const c = await newClient(page)
  await openHome(page, `/clients?id=${c.id}`)
  await expect(clientName(panel(page))).toHaveText(c.name)
  await page.getByRole('button', { name: 'Назад' }).tap()
  await expect(page.getByTestId('client-row').first()).toBeVisible()
  expect(path(page)).toBe('/clients')
})

test('шторка на карточке клиента: «назад» закрывает шторку, затем карточку', async ({ page }) => {
  const c = await newClient(page)
  await clientsFromOverview(page, c.name)
  await clientCard(page, c.name).tap()
  await expect(clientName(panel(page))).toHaveText(c.name)
  const add = page.getByRole('dialog', { name: 'Новый девайс' })
  const plus = () => page.getByRole('button', { name: '+ девайс', exact: true }).tap()
  await openSheet(page, add, plus)

  await page.goBack()
  await expect(add).toBeHidden()
  await expect(clientName(panel(page))).toHaveText(c.name)
  expect(path(page)).toBe(`/clients?id=${c.id}`)

  // Шторка, закрытая кнопкой, не оставляет в истории пустого шага.
  await openSheet(page, add, plus)
  await add.getByRole('button', { name: 'Отмена' }).tap()
  await expect(add).toBeHidden()
  await page.goBack()
  await expect(clientCard(page, c.name)).toBeVisible()
  expect(path(page)).toBe('/clients')
})

test('удаление клиента со шторки возвращает к списку, «назад» уходит с раздела', async ({ page }) => {
  const c = await newClient(page)
  await clientsFromOverview(page, c.name)
  await clientCard(page, c.name).tap()
  await panel(page).getByRole('button', { name: 'Удалить клиента' }).tap()
  const del = page.getByRole('dialog', { name: 'Удалить клиента?' })
  const accepted = page.waitForResponse(r => r.request().method() === 'DELETE' && r.url().endsWith(`/api/clients/${c.id}`))
  await del.getByRole('button', { name: 'Удалить' }).tap()
  const response = await accepted
  expect(response.status()).toBe(202)
  const { operation } = await response.json()
  expect(operation.status).toBe('pending')
  await expect(toast(page, 'Отключение выполняется')).toBeVisible()
  await waitAccess(page.request, operation)
  await expect(del).toBeHidden()
  await expect(page).toHaveURL(/\/clients$/)
  await expect(page.getByPlaceholder('Поиск...')).toBeVisible()
  await page.goBack()
  await expect(page).toHaveURL(/\/$/)
})

test('шторка ноды на Обзоре закрывается «назад», страница остаётся', async ({ page }) => {
  await openHome(page, '/')
  const sh = page.getByRole('dialog', { name: 'Entry' })
  await openSheet(page, sh, () => page.locator('.ov-nodes .node[data-tag="ru"]').tap())
  await page.goBack()
  await expect(sh).toBeHidden()
  await expect(page).toHaveURL(/\/$/)
  await expect(page.locator('.ov-nodes')).toBeVisible()
})

test('пункт меню профиля в шторке: переход без лишнего шага назад', async ({ page }) => {
  await openHome(page, '/')
  await page.getByRole('button', { name: /^Меню @/ }).tap()
  const menu = page.getByRole('dialog', { name: 'Меню профиля' })
  await expect(menu).toBeVisible()
  await menu.getByRole('menuitem', { name: 'Мой профиль' }).tap()
  await expect(page).toHaveURL(/\/settings$/)
  await page.goBack()
  await expect(page).toHaveURL(/\/$/)
  await expect(menu).toBeHidden()
  await page.goForward()
  await expect(page).toHaveURL(/\/settings$/)
})

test('«Выйти» в шторке меню ведёт на вход: закрытие шторки не перебивает переход', async ({ page }) => {
  // Гонка закрытия шторки и перехода ловилась не каждый раз: несколько выходов подряд.
  for (let i = 0; i < 5; i++) {
    await loginApi(page)
    await page.goto('/settings')
    await hydrated(page)
    await page.getByRole('button', { name: /^Меню @/ }).tap()
    await page.getByRole('menuitem', { name: 'Выйти' }).tap()
    await expect(page, `выход ${i + 1}`).toHaveURL(/\/login/)
    await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
  }
})
