import type { Page } from '@playwright/test'
import { expect, loginApi, test, uniq } from './support/fixtures'
import { clientCard, clientName } from './support/clients'
import type { AccessOperation } from '../shared/access-operation'

/**
 * Удаление выбранного клиента идёт (экзит не подтвердил), и выбрать вместо него
 * некого: среди видимых остались только те, чьё удаление идёт. Правая часть
 * пуста, и обновления списка не открывают удаляемого снова.
 */

interface Made { id: number, name: string }

/**
 * Свои клиенты с суффиксами `suffixes`; список на странице сужен до них. Любое
 * удаление подвешено: список отдаёт клиента замороженным, операция - pending.
 */
async function withHeldDeletes(page: Page, suffixes: string[]) {
  const prefix = uniq('pe')
  const made: Made[] = []
  for (const suffix of suffixes) {
    const name = `${prefix}-${suffix}`
    const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
    expect(r.status(), await r.text()).toBe(200)
    made.push({ id: (await r.json()).id, name })
  }
  const held = new Map<number, string>()
  const rows = new Map<number, { id: number, name: string, status: string }>()
  const details = new Map<number, Record<string, unknown>>()
  for (const m of made) {
    const saved = await page.request.get(`/api/clients/${m.id}`)
    expect(saved.status()).toBe(200)
    details.set(m.id, { ...await saved.json(), status: 'frozen' })
  }
  const state = { heldListed: 0 }
  await page.route(/\/api\/clients\/\d+$/, async (route) => {
    const id = Number(new URL(route.request().url()).pathname.split('/').pop())
    if (!made.some(m => m.id === id)) return route.fallback()
    // Пока удаление висит, клиент для страницы есть: карточка отдаёт его, а не 404.
    if (route.request().method() === 'GET' && held.has(id)) return route.fulfill({ status: 200, json: details.get(id) })
    if (route.request().method() !== 'DELETE') return route.fallback()
    const response = await route.fetch()
    held.set(id, (await response.json() as { operation: AccessOperation }).operation.id)
    await route.fulfill({ response })
  })
  await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const holding = held.size > 0
    const response = await route.fetch()
    const list: { id: number, name: string, status: string }[] = await response.json()
    const own = list.filter(x => x.name.startsWith(prefix))
    for (const x of own) if (!held.has(x.id)) rows.set(x.id, x)
    const json = [...own.filter(x => !held.has(x.id)), ...[...held.keys()].map(id => ({ ...rows.get(id)!, status: 'frozen' }))]
    await route.fulfill({ response, json }).then(() => {
      if (holding) state.heldListed++
    }, (e: Error) => {
      if (!e.message.includes('already handled')) throw e
    })
  })
  await page.route(/\/api\/access-operations\/[^/]+$/, async (route) => {
    const response = await route.fetch()
    const op: AccessOperation = await response.json()
    // Удаления для страницы не заканчиваются до конца теста.
    const hold = [...held.values()].includes(op.id)
    await route.fulfill({ response, json: hold ? { ...op, status: 'pending' } : op })
  })
  return { prefix, made, state }
}

async function deleteSelected(page: Page) {
  await page.getByTestId('client-detail').getByRole('button', { name: 'Удалить клиента' }).click()
  await page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click()
}

/** Записывать каждое имя, которое показывает карточка справа, начиная с этого момента. */
async function recordShown(page: Page) {
  await page.evaluate(() => {
    const w = window as unknown as { shownNames: string[] }
    w.shownNames = []
    const read = () => {
      const name = document.querySelector('[data-testid="client-detail"] [title="Имя клиента"]')?.textContent?.trim()
      if (name) w.shownNames.push(name)
    }
    new MutationObserver(read).observe(document.body, { subtree: true, childList: true, characterData: true })
  })
}
const shownNames = (page: Page) => page.evaluate(() => (window as unknown as { shownNames: string[] }).shownNames)

/** После удаления `y` справа пусто, и следующие 3+ с обновлений списка `y` не открывают. */
async function staysEmpty(page: Page, state: { heldListed: number }, y: Made) {
  const detail = page.getByTestId('client-detail')
  await expect(detail).toContainText('Выбери клиента в списке', { timeout: 10_000 })
  await recordShown(page)
  // Положительный контроль подмены: y в списке, обновления списка с ним приходят.
  await expect(clientCard(page, y.name)).toBeVisible()
  const listed = state.heldListed
  const since = Date.now()
  await expect.poll(() => state.heldListed, { timeout: 15_000 }).toBeGreaterThan(listed + 1)
  if (Date.now() - since < 3500) await page.waitForTimeout(3500 - (Date.now() - since))
  await expect(detail).toContainText('Выбери клиента в списке')
  await expect(clientName(detail)).toHaveCount(0)
  await expect(clientCard(page, y.name)).not.toHaveAttribute('aria-pressed', 'true')
  expect(await shownNames(page)).not.toContain(y.name)
}

test('поиск оставил одного y, удаление y висит: справа пусто, y не открывается снова', async ({ page }) => {
  await loginApi(page)
  const { prefix, made, state } = await withHeldDeletes(page, ['y'])
  const [y] = made as [Made]
  try {
    await page.goto('/clients')
    await page.getByPlaceholder('Поиск...').fill(prefix)
    await clientCard(page, y.name).click()
    await expect(clientName(page.getByTestId('client-detail'))).toHaveText(y.name)
    await deleteSelected(page)
    await staysEmpty(page, state, y)
  }
  finally {
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    for (const x of made) await page.request.delete(`/api/clients/${x.id}`)
  }
})

test('удаление x висит, удаление выбранного y висит: справа пусто, ни x, ни y не открываются', async ({ page }) => {
  await loginApi(page)
  const { prefix, made, state } = await withHeldDeletes(page, ['b-x', 'a-y'])
  const [x, y] = made as [Made, Made]
  try {
    await page.goto('/clients')
    await page.getByPlaceholder('Поиск...').fill(prefix)
    await clientCard(page, x.name).click()
    await expect(clientName(page.getByTestId('client-detail'))).toHaveText(x.name)
    await deleteSelected(page)
    await expect(clientName(page.getByTestId('client-detail'))).toHaveText(y.name, { timeout: 10_000 })
    await deleteSelected(page)
    await staysEmpty(page, state, y)
    expect(await shownNames(page)).not.toContain(x.name)
  }
  finally {
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    for (const x of made) await page.request.delete(`/api/clients/${x.id}`)
  }
})
