import type { Page } from '@playwright/test'
import { expect, loginApi, test, uniq } from './support/fixtures'
import { clientCard, clientName, createClientUi } from './support/clients'
import { waitAccess } from './support/access'
import type { AccessOperation } from '../shared/access-operation'

/**
 * Фильтр «Истекают»: в нём только активные клиенты со сроком в ближайшие дни.
 * После удаления выбран видимый в нём сосед, после создания новый виден строкой:
 * не попавший в фильтр новый сбрасывает его, попавший - нет.
 */

interface Made { id: number, name: string }

const DAY = 24 * 60 * 60 * 1000
/** Срок в окне «Истекают». */
const soon = () => new Date(Date.now() + 3 * DAY)

async function ownClients(page: Page, prefix: string, specs: { suffix: string, expiresAt?: Date, frozen?: boolean }[]) {
  const made: Made[] = []
  for (const s of specs) {
    const name = `${prefix}-${s.suffix}`
    const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null, expiresAt: s.expiresAt?.toISOString() ?? null } })
    expect(r.status(), await r.text()).toBe(200)
    const c = { id: (await r.json()).id, name }
    if (s.frozen) {
      const f = await page.request.patch(`/api/clients/${c.id}`, { data: { frozenManual: true } })
      expect(f.status(), await f.text()).toBe(202)
      await waitAccess(page.request, (await f.json()).operation)
    }
    made.push(c)
  }
  await page.route(/\/api\/clients(\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const response = await route.fetch()
    const list: { name: string }[] = await response.json()
    await route.fulfill({ response, json: list.filter(c => c.name.startsWith(prefix)) }).catch((e: Error) => {
      if (!e.message.includes('already handled')) throw e
    })
  })
  return made
}

const segment = (page: Page, name: string) => page.getByRole('button', { name: new RegExp(`^${name}`) })

async function openExpiring(page: Page) {
  await page.goto('/clients')
  await segment(page, 'Истекают').click()
  await expect(segment(page, 'Истекают')).toHaveClass(/\bon\b/)
}

async function select(page: Page, c: Made) {
  await clientCard(page, c.name).click()
  await expect(clientName(page.getByTestId('client-detail'))).toHaveText(c.name)
}

async function deleteSelected(page: Page, target: Made) {
  const panel = page.getByTestId('client-detail')
  await panel.getByRole('button', { name: 'Удалить клиента' }).click()
  const [response] = await Promise.all([
    page.waitForResponse(r => r.request().method() === 'DELETE' && new URL(r.url()).pathname === `/api/clients/${target.id}`),
    page.getByRole('dialog', { name: 'Удалить клиента?' }).getByRole('button', { name: 'Удалить' }).click(),
  ])
  expect(response.status()).toBe(202)
  const { operation } = await response.json() as { operation: AccessOperation }
  await waitAccess(page.request, operation)
  await expect(clientCard(page, target.name)).toHaveCount(0)
}

async function create(page: Page, name: string, expires?: Date): Promise<Made> {
  const ddmmyyyy = expires && [expires.getDate(), expires.getMonth() + 1, expires.getFullYear()]
    .map((n, i) => String(n).padStart(i < 2 ? 2 : 4, '0')).join('.')
  const [response] = await Promise.all([
    page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/clients'),
    createClientUi(page, name, { expires: ddmmyyyy }),
  ])
  return { id: (await response.json()).id, name }
}

async function expectShown(page: Page, c: Made) {
  await page.waitForLoadState('networkidle')
  await expect(clientName(page.getByTestId('client-detail'))).toHaveText(c.name)
  await expect(clientCard(page, c.name)).toBeVisible()
  await expect(clientCard(page, c.name)).toHaveAttribute('aria-pressed', 'true')
}

async function cleanup(page: Page, made: Made[]) {
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  for (const c of made) await page.request.delete(`/api/clients/${c.id}`)
}

test('удаление при «Истекают»: выбран видимый сосед, а не замороженный со сроком между ними', async ({ page }) => {
  await loginApi(page)
  const made = await ownClients(page, uniq('fe'), [
    { suffix: 'a', expiresAt: soon() },
    { suffix: 'b', expiresAt: soon(), frozen: true },
    { suffix: 'c', expiresAt: soon() },
  ])
  const [a, b, c] = made as [Made, Made, Made]
  try {
    await openExpiring(page)
    await expect(clientCard(page, c.name)).toBeVisible()
    await expect(clientCard(page, b.name)).toHaveCount(0)
    await select(page, a)
    await deleteSelected(page, a)
    await expectShown(page, c)
  }
  finally { await cleanup(page, made) }
})

test('удаление единственного истекающего: не выбран никто, клиент без срока не открыт', async ({ page }) => {
  await loginApi(page)
  const made = await ownClients(page, uniq('fe'), [
    { suffix: 'a', expiresAt: soon() },
    { suffix: 'b' },
  ])
  const [a, b] = made as [Made, Made]
  try {
    await openExpiring(page)
    await expect(clientCard(page, b.name)).toHaveCount(0)
    await select(page, a)
    await deleteSelected(page, a)
    await page.waitForLoadState('networkidle')
    const panel = page.getByTestId('client-detail')
    await expect(panel.getByText('Выбери клиента в списке')).toBeVisible()
    await expect(clientName(panel)).toHaveCount(0)
  }
  finally { await cleanup(page, made) }
})

test('создание клиента без срока при «Истекают»: фильтр сброшен, новый выбран и виден', async ({ page }) => {
  await loginApi(page)
  const prefix = uniq('fe')
  const made = await ownClients(page, prefix, [{ suffix: 'a', expiresAt: soon() }])
  try {
    await openExpiring(page)
    await expect(clientCard(page, made[0]!.name)).toBeVisible()
    made.push(await create(page, `${prefix}-b`))
    await expectShown(page, made[1]!)
    await expect(segment(page, 'Все')).toHaveClass(/\bon\b/)
  }
  finally { await cleanup(page, made) }
})

test('создание клиента со сроком в окне при «Истекают»: фильтр остался, новый выбран и виден', async ({ page }) => {
  await loginApi(page)
  const prefix = uniq('fe')
  const made = await ownClients(page, prefix, [{ suffix: 'a', expiresAt: soon() }, { suffix: 'c' }])
  try {
    await openExpiring(page)
    await expect(clientCard(page, made[1]!.name)).toHaveCount(0)
    made.push(await create(page, `${prefix}-b`, soon()))
    await expectShown(page, made[2]!)
    await expect(segment(page, 'Истекают')).toHaveClass(/\bon\b/)
    await expect(clientCard(page, made[1]!.name)).toHaveCount(0)
  }
  finally { await cleanup(page, made) }
})
