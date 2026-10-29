import type { Page } from '@playwright/test'
import { expect, loginApi, test, uniq } from './support/fixtures'
import { clientCard, clientName } from './support/clients'
import type { AccessOperation } from '../shared/access-operation'

/**
 * Удаление одного клиента ещё идёт (экзит не подтвердил), и он остаётся в списке
 * замороженным. Удаление его соседа не выбирает его взамен: соседом берётся тот,
 * чьё удаление не идёт, и удаляемый справа не открывается ни на миг.
 */

interface Made { id: number, name: string }

/**
 * Три своих клиента a, b, c; список на странице сужен до них. Удаление `c`
 * подвешено: список отдаёт его замороженным, операция для страницы - pending.
 */
async function threeWithPending(page: Page) {
  const prefix = uniq('pn')
  const made: Made[] = []
  for (const suffix of ['a', 'b', 'c']) {
    const name = `${prefix}-${suffix}`
    const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
    expect(r.status(), await r.text()).toBe(200)
    made.push({ id: (await r.json()).id, name })
  }
  const c = made[2]!
  const state = { cDeleted: false, operationId: '', frozenListed: 0 }
  let row: { id: number, name: string, status: string } | undefined
  const saved = await page.request.get(`/api/clients/${c.id}`)
  expect(saved.status()).toBe(200)
  const detail = { ...await saved.json(), status: 'frozen' }
  await page.route(new RegExp(`/api/clients/${c.id}$`), async (route) => {
    // Пока удаление висит, клиент для страницы есть: карточка отдаёт его, а не 404.
    if (route.request().method() === 'GET' && state.cDeleted) return route.fulfill({ status: 200, json: detail })
    if (route.request().method() !== 'DELETE') return route.fallback()
    const response = await route.fetch()
    state.operationId = (await response.json() as { operation: AccessOperation }).operation.id
    state.cDeleted = true
    await route.fulfill({ response })
  })
  await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const pending = state.cDeleted
    const response = await route.fetch()
    const list: { id: number, name: string, status: string }[] = await response.json()
    const own = list.filter(x => x.name.startsWith(prefix))
    const seen = own.find(x => x.id === c.id)
    if (!pending && seen) row = seen
    if (pending) own.splice(0, own.length, ...own.filter(x => x.id !== c.id), { ...(seen ?? row!), status: 'frozen' })
    await route.fulfill({ response, json: own }).then(() => {
      if (pending) state.frozenListed++
    }, (e: Error) => {
      if (!e.message.includes('already handled')) throw e
    })
  })
  await page.route(/\/api\/access-operations\/[^/]+$/, async (route) => {
    const response = await route.fetch()
    const op: AccessOperation = await response.json()
    // Удаление c для страницы не заканчивается до конца теста.
    const hold = state.operationId !== '' && op.id === state.operationId
    await route.fulfill({ response, json: hold ? { ...op, status: 'pending' } : op })
  })
  return { prefix, made, state }
}

async function select(page: Page, c: Made) {
  await clientCard(page, c.name).click()
  await expect(clientName(page.getByTestId('client-detail'))).toHaveText(c.name)
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

for (const view of ['all', 'search'] as const) {
  const what = view === 'all' ? '«Все»' : 'поиск'
  test(`${what}: удаление c висит, удаление b выбирает a, а не удаляемого c`, async ({ page }) => {
    await loginApi(page)
    const { prefix, made, state } = await threeWithPending(page)
    const [a, b, c] = made as [Made, Made, Made]
    try {
      await page.goto('/clients')
      if (view === 'search') await page.getByPlaceholder('Поиск...').fill(prefix)
      for (const x of made) await expect(clientCard(page, x.name)).toBeVisible()
      await select(page, c)
      await deleteSelected(page)
      // Положительный контроль подмены: c в списке замороженным, выбор ушёл на b.
      await expect.poll(() => state.frozenListed, { timeout: 15_000 }).toBeGreaterThan(0)
      await expect(clientCard(page, c.name)).toBeVisible()
      await expect(clientName(page.getByTestId('client-detail'))).toHaveText(b.name)

      await recordShown(page)
      await deleteSelected(page)
      await expect(clientName(page.getByTestId('client-detail'))).toHaveText(a.name)
      await expect(clientCard(page, a.name)).toHaveAttribute('aria-pressed', 'true')
      // Список с c ещё приходит, а выбор держится на a и c не открывался ни разу.
      const listed = state.frozenListed
      await expect.poll(() => state.frozenListed, { timeout: 15_000 }).toBeGreaterThan(listed + 1)
      await expect(clientName(page.getByTestId('client-detail'))).toHaveText(a.name)
      expect(await shownNames(page)).not.toContain(c.name)
    }
    finally {
      await page.unrouteAll({ behavior: 'ignoreErrors' })
      for (const x of made) await page.request.delete(`/api/clients/${x.id}`)
    }
  })
}
