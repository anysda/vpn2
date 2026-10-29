import type { Page } from '@playwright/test'
import { expect, loginApi, test, uniq } from './support/fixtures'
import { clientCard, clientName } from './support/clients'
import { waitAccess } from './support/access'
import type { AccessOperation } from '../shared/access-operation'

/**
 * После удаления выбранного клиента при активном поиске или фильтре сосед
 * берётся из видимого списка: клиент, скрытый поиском или фильтром, не
 * выбирается; не осталось видимых - не выбран никто.
 */

interface Made { id: number, name: string }

/** Свои клиенты с именами `<префикс>-<суффикс>`; список на странице сужен до них. */
async function ownClients(page: Page, suffixes: string[]) {
  const prefix = uniq('df')
  const made: Made[] = []
  for (const suffix of suffixes) {
    const name = `${prefix}-${suffix}`
    const r = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
    expect(r.status(), await r.text()).toBe(200)
    made.push({ id: (await r.json()).id, name })
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

async function freeze(page: Page, c: Made) {
  const r = await page.request.patch(`/api/clients/${c.id}`, { data: { frozenManual: true } })
  expect(r.status(), await r.text()).toBe(202)
  await waitAccess(page.request, (await r.json()).operation)
}

async function select(page: Page, c: Made) {
  await clientCard(page, c.name).click()
  await expect(clientName(page.getByTestId('client-detail'))).toHaveText(c.name)
}

/** Удалить выбранного `target` из его карточки и дождаться конца операции. */
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

/** Выбран `expected`: карточка его, строка видна и подсвечена; скрытые не открыты. */
async function expectSelected(page: Page, expected: Made) {
  const panel = page.getByTestId('client-detail')
  await page.waitForLoadState('networkidle')
  await expect(clientName(panel)).toHaveText(expected.name)
  await expect(clientCard(page, expected.name)).toBeVisible()
  await expect(clientCard(page, expected.name)).toHaveAttribute('aria-pressed', 'true')
}

async function cleanup(page: Page, made: Made[]) {
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  for (const c of made) await page.request.delete(`/api/clients/${c.id}`)
}

test('удаление при поиске: выбран видимый сосед, а не скрытый поиском между ними', async ({ page }) => {
  await loginApi(page)
  const made = await ownClients(page, ['a-keep', 'b-hide', 'c-keep'])
  const [a, b, c] = made as [Made, Made, Made]
  try {
    await page.goto('/clients')
    await page.getByPlaceholder('Поиск...').fill('keep')
    await expect(clientCard(page, b.name)).toHaveCount(0)
    await select(page, a)
    await deleteSelected(page, a)
    await expectSelected(page, c)
  }
  finally { await cleanup(page, made) }
})

test('удаление при фильтре «Заморожены»: выбран видимый сосед, а не скрытый фильтром', async ({ page }) => {
  await loginApi(page)
  const made = await ownClients(page, ['a', 'b', 'c'])
  const [a, b, c] = made as [Made, Made, Made]
  try {
    await freeze(page, a)
    await freeze(page, c)
    await page.goto('/clients')
    await page.getByRole('button', { name: /^Заморожены/ }).click()
    await expect(clientCard(page, b.name)).toHaveCount(0)
    await expect(clientCard(page, c.name)).toBeVisible()
    await select(page, a)
    await deleteSelected(page, a)
    await expectSelected(page, c)
  }
  finally { await cleanup(page, made) }
})

test('удаление последнего видимого при поиске: выбран видимый над ним, а не скрытый под ним', async ({ page }) => {
  await loginApi(page)
  const made = await ownClients(page, ['a-keep', 'b-keep', 'c-hide'])
  const [a, b, c] = made as [Made, Made, Made]
  try {
    await page.goto('/clients')
    await page.getByPlaceholder('Поиск...').fill('keep')
    await expect(clientCard(page, c.name)).toHaveCount(0)
    await select(page, b)
    await deleteSelected(page, b)
    await expectSelected(page, a)
  }
  finally { await cleanup(page, made) }
})

test('удаление единственного видимого при поиске: не выбран никто, скрытый не открыт', async ({ page }) => {
  await loginApi(page)
  const made = await ownClients(page, ['a-keep', 'b-hide'])
  const [a] = made as [Made, Made]
  try {
    await page.goto('/clients')
    await page.getByPlaceholder('Поиск...').fill('keep')
    await select(page, a)
    await deleteSelected(page, a)
    await page.waitForLoadState('networkidle')
    const panel = page.getByTestId('client-detail')
    await expect(panel.getByText('Выбери клиента в списке')).toBeVisible()
    await expect(clientName(panel)).toHaveCount(0)
  }
  finally { await cleanup(page, made) }
})

test('удаление без поиска и фильтра: выбран стоявший под ним', async ({ page }) => {
  await loginApi(page)
  const made = await ownClients(page, ['a', 'b', 'c'])
  const [a, b] = made as [Made, Made, Made]
  try {
    await page.goto('/clients')
    await select(page, a)
    await deleteSelected(page, a)
    await expectSelected(page, b)
  }
  finally { await cleanup(page, made) }
})
