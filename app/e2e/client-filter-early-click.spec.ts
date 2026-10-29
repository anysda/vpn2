import type { Page } from '@playwright/test'
import { expect, hydrated, loginApi, test, uniq } from './support/fixtures'
import { clientCard } from './support/clients'
import { waitAccess } from './support/access'

/**
 * Страница клиентов приходит с сервера готовой на вид: фильтры и поиск видны
 * раньше, чем Nuxt гидратирует её и повесит обработчики. Человек, кликнувший
 * «Истекают» или начавший печатать в поиск в это окно, должен получить свой
 * фильтр после готовности страницы, а не тихо потерянное действие.
 * Медленную гидратацию задаём детерминированно: JS-чанки придержаны.
 */

interface Made { id: number, name: string }

const DAY = 24 * 60 * 60 * 1000
const soon = () => new Date(Date.now() + 3 * DAY)
/** Сколько держать JS: окно между отрисовкой сервером и гидратацией. */
const HOLD_MS = 1500

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

/** Придержать скрипты Nuxt: разметка и стили уже на экране, гидратации ещё нет. */
async function slowHydration(page: Page) {
  await page.route(/\/_nuxt\/.*\.js(\?.*)?$/, async (route) => {
    await new Promise(r => setTimeout(r, HOLD_MS))
    await route.continue().catch(() => {})
  })
}

/** Открыть страницу и вернуть управление, как только пришла разметка. */
async function openEarly(page: Page) {
  await page.goto('/clients', { waitUntil: 'commit' })
}

const segment = (page: Page, name: string) => page.getByRole('button', { name: new RegExp(`^${name}`) })
const searchBox = (page: Page) => page.getByLabel('Поиск клиентов')

async function cleanup(page: Page, made: Made[]) {
  await page.unrouteAll({ behavior: 'ignoreErrors' })
  for (const c of made) await page.request.delete(`/api/clients/${c.id}`)
}

test('клик «Истекают» до гидратации: после готовности фильтр включён, в списке только истекающие', async ({ page }) => {
  await loginApi(page)
  const made = await ownClients(page, uniq('fec'), [
    { suffix: 'a', expiresAt: soon() },
    { suffix: 'b' },
  ])
  const [a, b] = made as [Made, Made]
  try {
    await slowHydration(page)
    await openEarly(page)
    await segment(page, 'Истекают').click()
    await hydrated(page)
    await expect(clientCard(page, a.name)).toBeVisible()
    await expect(segment(page, 'Истекают')).toHaveClass(/\bon\b/)
    await expect(segment(page, 'Все')).not.toHaveClass(/\bon\b/)
    await expect(clientCard(page, b.name)).toHaveCount(0)
  }
  finally { await cleanup(page, made) }
})

test('клик «Заморожены» до гидратации: после готовности фильтр включён, в списке только замороженные', async ({ page }) => {
  await loginApi(page)
  const made = await ownClients(page, uniq('fec'), [
    { suffix: 'a', frozen: true },
    { suffix: 'b' },
  ])
  const [a, b] = made as [Made, Made]
  try {
    await slowHydration(page)
    await openEarly(page)
    await segment(page, 'Заморожены').click()
    await hydrated(page)
    await expect(clientCard(page, a.name)).toBeVisible()
    await expect(segment(page, 'Заморожены')).toHaveClass(/\bon\b/)
    await expect(segment(page, 'Все')).not.toHaveClass(/\bon\b/)
    await expect(clientCard(page, b.name)).toHaveCount(0)
  }
  finally { await cleanup(page, made) }
})

test('ввод в поиск до гидратации: после готовности текст на месте и список отфильтрован', async ({ page }) => {
  await loginApi(page)
  const made = await ownClients(page, uniq('fec'), [
    { suffix: 'alpha' },
    { suffix: 'beta' },
  ])
  const [a, b] = made as [Made, Made]
  try {
    await slowHydration(page)
    await openEarly(page)
    await searchBox(page).fill(a.name)
    await hydrated(page)
    await expect(clientCard(page, a.name)).toBeVisible()
    await expect(searchBox(page)).toHaveValue(a.name)
    await expect(clientCard(page, b.name)).toHaveCount(0)
  }
  finally { await cleanup(page, made) }
})

test('клик «Истекают» на готовой странице: фильтр включён, в списке только истекающие', async ({ page }) => {
  await loginApi(page)
  const made = await ownClients(page, uniq('fec'), [
    { suffix: 'a', expiresAt: soon() },
    { suffix: 'b' },
  ])
  const [a, b] = made as [Made, Made]
  try {
    await page.goto('/clients')
    await expect(clientCard(page, b.name)).toBeVisible()
    await segment(page, 'Истекают').click()
    await expect(segment(page, 'Истекают')).toHaveClass(/\bon\b/)
    await expect(clientCard(page, a.name)).toBeVisible()
    await expect(clientCard(page, b.name)).toHaveCount(0)
  }
  finally { await cleanup(page, made) }
})
