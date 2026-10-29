import type { Page } from '@playwright/test'
import { expect, loginApi, test, uniq } from './support/fixtures'
import { clientCard, clientName, createClientUi } from './support/clients'
import { waitAccess } from './support/access'

/**
 * Новый клиент после создания выбран и виден строкой в списке: поиск и фильтр,
 * которые его скрывают, сбрасываются; не скрывают - остаются как были.
 */

interface Made { id: number, name: string }

/** Свой клиент `<префикс>-<суффикс>`; список на странице сужен до префикса. */
async function ownClients(page: Page, prefix: string, suffixes: string[]) {
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

/** Создать клиента формой; вернуть его вместе с id из ответа. */
async function create(page: Page, name: string): Promise<Made> {
  const [response] = await Promise.all([
    page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/clients'),
    createClientUi(page, name),
  ])
  return { id: (await response.json()).id, name }
}

/** Новый выбран: карточка его, строка видна и подсвечена. */
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

const search = (page: Page) => page.getByPlaceholder('Поиск...')
const segment = (page: Page, name: string) => page.getByRole('button', { name: new RegExp(`^${name}`) })

test('создание при поиске, который нового не находит: поиск сброшен, новый выбран и виден', async ({ page }) => {
  await loginApi(page)
  const prefix = uniq('cf')
  const made = await ownClients(page, prefix, ['a-keep'])
  try {
    await page.goto('/clients')
    await search(page).fill('keep')
    await expect(clientCard(page, made[0]!.name)).toBeVisible()
    made.push(await create(page, `${prefix}-b-new`))
    await expectShown(page, made[1]!)
    await expect(search(page)).toHaveValue('')
  }
  finally { await cleanup(page, made) }
})

test('создание при фильтре «Заморожены»: фильтр сброшен, новый выбран и виден', async ({ page }) => {
  await loginApi(page)
  const prefix = uniq('cf')
  const made = await ownClients(page, prefix, ['a'])
  try {
    const r = await page.request.patch(`/api/clients/${made[0]!.id}`, { data: { frozenManual: true } })
    expect(r.status(), await r.text()).toBe(202)
    await waitAccess(page.request, (await r.json()).operation)
    await page.goto('/clients')
    await segment(page, 'Заморожены').click()
    await expect(clientCard(page, made[0]!.name)).toBeVisible()
    made.push(await create(page, `${prefix}-b`))
    await expectShown(page, made[1]!)
    await expect(segment(page, 'Все')).toHaveClass(/\bon\b/)
  }
  finally { await cleanup(page, made) }
})

test('создание при поиске, который нового находит: поиск остался, новый выбран и виден', async ({ page }) => {
  await loginApi(page)
  const prefix = uniq('cf')
  const made = await ownClients(page, prefix, ['a-keep', 'c-hide'])
  try {
    await page.goto('/clients')
    await search(page).fill('keep')
    await expect(clientCard(page, made[1]!.name)).toHaveCount(0)
    made.push(await create(page, `${prefix}-b-keep`))
    await expectShown(page, made[2]!)
    await expect(search(page)).toHaveValue('keep')
    await expect(clientCard(page, made[1]!.name)).toHaveCount(0)
  }
  finally { await cleanup(page, made) }
})
