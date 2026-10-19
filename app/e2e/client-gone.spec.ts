import type { Page } from '@playwright/test'
import { expect, loginApi, test, uniq } from './support/fixtures'
import { clientCard, clientName } from './support/clients'
import { waitAccess } from './support/access'

/**
 * Клиента удаляет второй админ (или тест через API), пока панель грузит его
 * карточку: GET карточки и трафика приходят 404. Панель молча убирает его из
 * выбора и показывает следующего, без тоста об ошибке.
 */

/** Два своих клиента; список на странице сужен до них, первым выбирается first. */
async function twoClients(page: Page) {
  const prefix = uniq('gone')
  const ids: number[] = []
  for (const suffix of ['a', 'b']) {
    const r = await page.request.post('/api/clients', { data: { name: `${prefix}-${suffix}`, deviceLimit: null } })
    expect(r.status(), await r.text()).toBe(200)
    ids.push((await r.json()).id)
  }
  await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const response = await route.fetch()
    const list: { name: string }[] = await response.json()
    // Новый опрос списка обрывает прежний запрос (useFetch отменяет его): отвечать уже некому.
    await route.fulfill({ response, json: list.filter(c => c.name.startsWith(prefix)) }).catch((e: Error) => {
      if (!e.message.includes('already handled')) throw e
    })
  })
  return { first: { id: ids[0]!, name: `${prefix}-a` }, second: { id: ids[1]!, name: `${prefix}-b` } }
}

function toasts(page: Page) {
  return page.getByRole('region', { includeHidden: true }).locator('ol > li')
}

test('клиента удалили до загрузки карточки: панель молча выбирает следующего', async ({ page }) => {
  await loginApi(page)
  const { first, second } = await twoClients(page)
  try {
    const gone: string[] = []
    page.on('response', (r) => {
      if (r.status() === 404 && r.url().includes(`/api/clients/${first.id}`)) gone.push(r.url())
    })
    // Первый запрос карточки выбранного клиента: удалить его и
    // только потом пропустить запрос к серверу — он ответит настоящим 404.
    let deleted: Promise<unknown> | null = null
    await page.route(new RegExp(`/api/clients/${first.id}$`), async (route) => {
      deleted ??= page.request.delete(`/api/clients/${first.id}`).then(async r => waitAccess(page.request, (await r.json()).operation))
      await deleted
      await route.continue()
    })

    await page.goto('/clients')
    const panel = page.getByTestId('client-detail')
    await expect(clientName(panel)).toHaveText(second.name)
    // Без повторов: тост гаснет сам, ожидание нуля его бы пересидело.
    expect(await toasts(page).allInnerTexts(), 'тост при пропавшем клиенте').toEqual([])
    await expect(clientCard(page, second.name)).toHaveAttribute('aria-pressed', 'true')
    await expect(clientCard(page, first.name)).toHaveCount(0)
    await page.waitForLoadState('networkidle')
    // Положительный контроль: гонка действительно случилась.
    expect(gone.length, 'карточка удалённого клиента не запрашивалась').toBeGreaterThan(0)
    expect(await toasts(page).allInnerTexts(), 'тост при пропавшем клиенте').toEqual([])
  }
  finally {
    await page.unrouteAll({ behavior: 'wait' })
    await page.request.delete(`/api/clients/${first.id}`)
    await page.request.delete(`/api/clients/${second.id}`)
  }
})

test('удаление выбранного клиента из своей панели не запрашивает его снова', async ({ page }) => {
  await loginApi(page)
  const { first, second } = await twoClients(page)
  try {
    const missing: string[] = []
    page.on('response', (r) => {
      if (r.status() === 404) missing.push(`${r.request().method()} ${r.url()}`)
    })
    await page.goto('/clients')
    const panel = page.getByTestId('client-detail')
    await expect(clientName(panel)).toHaveText(first.name)
    await panel.getByRole('button', { name: 'Удалить клиента' }).click()
    await page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click()
    await expect(clientName(panel)).toHaveText(second.name)
    await expect(clientCard(page, first.name)).toHaveCount(0)
    await page.waitForLoadState('networkidle')
    expect(missing).toEqual([])
  }
  finally {
    await page.unrouteAll({ behavior: 'wait' })
    await page.request.delete(`/api/clients/${first.id}`)
    await page.request.delete(`/api/clients/${second.id}`)
  }
})

test('запоздалый список после удаления не выбирает удалённого клиента снова', async ({ page }) => {
  await loginApi(page)
  const { first, second } = await twoClients(page)
  try {
    const missing: string[] = []
    page.on('response', (r) => {
      if (r.status() === 404) missing.push(`${r.request().method()} ${r.url()}`)
    })
    // Удаление подтвердилось, а ответ списка, начатого раньше, ещё несёт
    // удалённого клиента. Списки с DELETE до подтверждения и 4 с после
    // отвечают снимком до удаления: подтверждение замечает карточка клиента,
    // а страница в этом окне видит старый список.
    let snapshot: unknown = null
    let deleting = false
    let confirmedAt = 0
    page.on('response', async (r) => {
      if (!deleting && r.request().method() === 'GET' && /\/api\/clients(\?.*)?$/.test(r.url()) && r.ok()) snapshot = await r.json().catch(() => snapshot)
    })
    await page.route(/\/api\/access-operations\/[^/]+$/, async (route) => {
      const response = await route.fetch()
      const operation: { status: string } = await response.json()
      if (operation.status === 'confirmed') confirmedAt ||= Date.now()
      await route.fulfill({ response })
    })
    await page.route(new RegExp(`/api/clients/${first.id}$`), async (route) => {
      if (route.request().method() === 'DELETE') deleting = true
      await route.fallback()
    })
    await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
      const stale = deleting && (!confirmedAt || Date.now() < confirmedAt + 4_000)
      if (route.request().method() !== 'GET' || !stale) return route.fallback()
      await route.fulfill({ json: snapshot }).catch((e: Error) => {
        if (!e.message.includes('already handled')) throw e
      })
    })
    await page.goto('/clients')
    const panel = page.getByTestId('client-detail')
    await expect(clientName(panel)).toHaveText(first.name)
    await panel.getByRole('button', { name: 'Удалить клиента' }).click()
    await page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click()
    await expect.poll(() => confirmedAt > 0, { timeout: 45_000 }).toBe(true)
    await expect(clientName(panel)).toHaveText(second.name)
    await expect(clientCard(page, first.name)).toHaveCount(0, { timeout: 15_000 })
    await page.waitForLoadState('networkidle')
    expect(missing).toEqual([])
    await expect(clientName(panel)).toHaveText(second.name)
  }
  finally {
    await page.unrouteAll({ behavior: 'wait' })
    await page.request.delete(`/api/clients/${first.id}`)
    await page.request.delete(`/api/clients/${second.id}`)
  }
})
