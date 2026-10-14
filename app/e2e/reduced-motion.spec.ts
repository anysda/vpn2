import type { Page } from '@playwright/test'
import { expect, hydrated, loginApi, test, uniq } from './support/fixtures'
import { clientCard } from './support/clients'

// Бесконечные анимации (ореолы «в сети», тревоги), которые сейчас бегут на странице.
function looping(page: Page) {
  return page.evaluate(() => document.getAnimations()
    .filter(a => a.playState === 'running' && a.effect?.getComputedTiming().iterations === Infinity)
    .map(a => `${(a as CSSAnimation).animationName ?? a.constructor.name} ${(a.effect as KeyframeEffect).pseudoElement ?? ''}`))
}

// Клиент «в сети» без настоящего трафика: ответ трафика подменяется в браузере.
async function onlineClient(page: Page) {
  await loginApi(page)
  const name = uniq('motion')
  const created = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
  expect(created.status()).toBe(200)
  const { id } = await created.json()
  await page.route(url => url.pathname === '/api/clients/traffic', route => route.fulfill({
    json: { [id]: { rxBytes: 1e9, txBytes: 1e9, dayRx: 1e9, dayTx: 1e9, recentBytes: 1e8, online: true } },
  }))
  return { id, name }
}

async function openOnline(page: Page, name: string) {
  await page.goto('/clients')
  await hydrated(page)
  await page.getByPlaceholder('Поиск...').fill(name)
  await expect(clientCard(page, name).getByTestId('client-online')).toHaveClass(/\bon\b/)
}

test.describe('уменьшить движение', () => {
  test.use({ reducedMotion: 'reduce' })

  test('на Обзоре и в Клиентах нет бесконечных анимаций', async ({ page }) => {
    const { id, name } = await onlineClient(page)
    try {
      await page.goto('/')
      await hydrated(page)
      await page.waitForTimeout(3000)
      expect(await looping(page)).toEqual([])
      await openOnline(page, name)
      expect(await looping(page)).toEqual([])
    }
    finally {
      await page.request.delete(`/api/clients/${id}`)
    }
  })
})

test('без «уменьшить движение» ореол «в сети» в Клиентах бежит', async ({ page }) => {
  const { id, name } = await onlineClient(page)
  try {
    await openOnline(page, name)
    expect(await looping(page)).toContain('clp-halo ::after')
  }
  finally {
    await page.request.delete(`/api/clients/${id}`)
  }
})
