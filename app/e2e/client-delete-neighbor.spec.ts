import type { Page } from '@playwright/test'
import { expect, loginApi, test, uniq } from './support/fixtures'
import { clientCard, clientName } from './support/clients'
import { waitAccess } from './support/access'
import type { AccessOperation } from '../shared/access-operation'

/**
 * После удаления выбранного клиента панель выбирает того, кто стоял под ним
 * (удалён последний - того, кто над ним). Свежий список без удалённого может
 * прийти раньше, чем карточка дождётся подтверждения операции удаления, -
 * выбор от этого не меняется.
 */

/** Три своих клиента a, b, c; список на странице сужен до них. */
async function threeClients(page: Page) {
  const prefix = uniq('nb')
  const made: { id: number, name: string }[] = []
  for (const suffix of ['a', 'b', 'c']) {
    const name = `${prefix}-${suffix}`
    const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
    expect(r.status(), await r.text()).toBe(200)
    made.push({ id: (await r.json()).id, name })
  }
  return { prefix, made }
}

/**
 * Удалить выбранного `target`: список после DELETE уже без него, а `confirmed`
 * операции страница может получить только после этого списка.
 */
async function deleteBeforeConfirmed(page: Page, prefix: string, target: { id: number, name: string }) {
  const deleted: { operation?: Promise<AccessOperation> } = {}
  let listedWithout = 0
  let confirmedAt = 0
  await page.route(new RegExp(`/api/clients/${target.id}$`), async (route) => {
    if (route.request().method() !== 'DELETE') return route.fallback()
    const response = await route.fetch()
    deleted.operation = response.json().then((body: { operation: AccessOperation }) => body.operation)
    await route.fulfill({ response })
  })
  await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const response = await route.fetch()
    const list: { id: number, name: string }[] = await response.json()
    const own = list.filter(c => c.name.startsWith(prefix) && !(deleted.operation && c.id === target.id))
    // Новый опрос списка обрывает прежний запрос (useFetch отменяет его): отвечать уже некому.
    await route.fulfill({ response, json: own }).then(() => {
      if (deleted.operation) listedWithout ||= Date.now()
    }, (e: Error) => {
      if (!e.message.includes('already handled')) throw e
    })
  })
  await page.route(/\/api\/access-operations\/[^/]+$/, async (route) => {
    const response = await route.fetch()
    const state: AccessOperation = await response.json()
    // Список без удалённого ещё не дошёл до страницы: подтверждение придержать.
    const early = state.status === 'confirmed' && (!listedWithout || Date.now() < listedWithout + 1_000)
    if (state.status === 'confirmed' && !early) confirmedAt ||= Date.now()
    await route.fulfill({ response, json: early ? { ...state, status: 'pending' } : state })
  })
  const panel = page.getByTestId('client-detail')
  await panel.getByRole('button', { name: 'Удалить клиента' }).click()
  await page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click()
  await expect.poll(() => listedWithout > 0, { timeout: 15_000 }).toBe(true)
  await expect(clientCard(page, target.name)).toHaveCount(0)
  // Сервер довёл удаление до конца: дальше страница видит только итоговое состояние.
  await waitAccess(page.request, await deleted.operation!)
  // Положительный контроль: список без удалённого дошёл до страницы раньше подтверждения.
  expect(!confirmedAt || confirmedAt > listedWithout, 'подтверждение пришло раньше списка').toBe(true)
}

for (const at of [1, 2]) {
  const what = at === 1 ? 'из середины: выбран стоявший под ним' : 'последнего: выбран стоявший над ним'
  test(`удаление клиента ${what}, хотя список обновился до подтверждения`, async ({ page }, testInfo) => {
    await loginApi(page)
    const { prefix, made } = await threeClients(page)
    const target = made[at]!
    const expected = made[at === 2 ? 1 : 2]!
    try {
      const missing: string[] = []
      page.on('response', (r) => {
        if (r.status() === 404) missing.push(`${r.request().method()} ${r.url()}`)
      })
      await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
        if (route.request().method() !== 'GET') return route.fallback()
        const response = await route.fetch()
        const list: { name: string }[] = await response.json()
        await route.fulfill({ response, json: list.filter(c => c.name.startsWith(prefix)) }).catch((e: Error) => {
          if (!e.message.includes('already handled')) throw e
        })
      })
      await page.goto('/clients')
      const panel = page.getByTestId('client-detail')
      await expect(clientName(panel)).toHaveText(made[0]!.name)
      await clientCard(page, target.name).click()
      await expect(clientName(panel)).toHaveText(target.name)

      await deleteBeforeConfirmed(page, prefix, target)
      await expect(clientName(panel)).toHaveText(expected.name)
      await expect(clientCard(page, expected.name)).toHaveAttribute('aria-pressed', 'true')
      await page.waitForLoadState('networkidle')
      await expect(clientName(panel)).toHaveText(expected.name)
      expect(missing, 'GET удалённого клиента с 404').toEqual([])
      await page.screenshot({ path: testInfo.outputPath('after-delete.png') })
    }
    finally {
      // Не ждать обработчиков: route.fetch() запроса, который страница уже оборвала, может не вернуться.
      await page.unrouteAll({ behavior: 'ignoreErrors' })
      for (const c of made) await page.request.delete(`/api/clients/${c.id}`)
    }
  })
}
