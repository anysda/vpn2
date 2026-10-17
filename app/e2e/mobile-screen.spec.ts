import type { Page } from '@playwright/test'
import { expect, NODES, test } from './support/fixtures'
import { openHome } from './support/clients'
import { orderBreaks } from './support/order'

// Телефон и «Настроить экран»: пункт меню, порядок блоков Обзора, фокус под шапкой.
test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

const defaults = { left: ['traffic', 'nodes', 'activity'], right: [], hidden: [] }
const headBottom = (page: Page) => page.locator('.wrap>header').evaluate(el => el.getBoundingClientRect().bottom)

/** Пункты меню профиля: подписи и адреса по порядку. */
function menuItems(menu: ReturnType<Page['locator']>) {
  return menu.locator('[role="menuitem"]').evaluateAll(els => els.map(e => `${e.textContent?.trim()} ${e.getAttribute('href') ?? ''}`.trim()))
}

test('шторка профиля: те же пункты, что в меню на ПК, «Настроить экран» открывает /screen', async ({ page }) => {
  await openHome(page)
  const desktop = await menuItems(page.locator('.wrap>header .pop.menu'))
  await page.getByRole('button', { name: /^Меню @/ }).tap()
  const sheet = page.locator('.bsh.on')
  await expect(sheet).toBeVisible()
  expect(await menuItems(sheet), 'пункты шторки = пункты меню ПК').toEqual(desktop)
  await sheet.getByRole('menuitem', { name: 'Настроить экран' }).tap()
  await expect(page).toHaveURL(u => u.pathname === '/screen')
  await expect(sheet).toHaveCount(0)
  const main = page.locator('main.tab.on.scr')
  await expect(main).toBeVisible()
  // кнопки страницы видны целиком: ниже шапки, в окне
  const bottom = await headBottom(page)
  for (const c of await main.locator('.head').locator('button, a').all()) {
    const r = (await c.boundingBox())!
    expect(r.y, `${await c.textContent()}: под шапкой`).toBeGreaterThanOrEqual(bottom)
    expect(r.x + r.width, `${await c.textContent()}: в окне`).toBeLessThanOrEqual(402)
  }
})

test.describe('порядок блоков Обзора из «Настроить экран»', () => {
  test.afterEach(({ page }) => page.request.put('/api/me/layout', { data: defaults }).then(() => undefined))

  // каждая перестановка и скрытие: блоки на телефоне в порядке /screen, разметка — как на экране
  const layouts: { on: string[], hidden: string[] }[] = [
    { on: ['activity', 'traffic', 'nodes'], hidden: [] },
    { on: ['activity', 'nodes', 'traffic'], hidden: [] },
    { on: ['nodes', 'traffic', 'activity'], hidden: [] },
    { on: ['nodes', 'activity', 'traffic'], hidden: [] },
    { on: ['traffic', 'activity', 'nodes'], hidden: [] },
    { on: ['traffic', 'nodes', 'activity'], hidden: [] },
    { on: ['activity', 'nodes'], hidden: ['traffic'] },
    { on: ['traffic'], hidden: ['nodes', 'activity'] },
  ]
  // первый блок модуля на экране
  const lead: Record<string, string> = { traffic: '.ov .ev', nodes: '.ov-nodes', activity: '.ov-activity' }

  for (const { on, hidden } of layouts) {
    test(`${on.join(', ')}${hidden.length ? ` (скрыто ${hidden.join(', ')})` : ''}`, async ({ page }) => {
      await openHome(page)
      const saved = await page.request.put('/api/me/layout', { data: { left: on, right: [], hidden } })
      expect(saved.ok()).toBe(true)
      await page.reload()
      if (on.includes('nodes')) for (const tag of NODES) await expect(page.locator(`.ov-nodes .node[data-tag="${tag}"]`)).toBeVisible()
      const main = page.locator('main.tab.on')
      await expect(main.locator('[data-blk]').first()).toBeVisible()
      expect(await main.locator('[data-blk]').evaluateAll(els => els.map(e => e.getAttribute('data-blk'))), 'модули в разметке').toEqual(on)
      for (const id of hidden) await expect(page.locator(lead[id]!)).toHaveCount(0)
      const tops = []
      for (const id of on) tops.push(Math.round((await page.locator(lead[id]!).first().boundingBox())!.y))
      expect(tops, `${on.join(', ')}: сверху вниз`).toEqual([...tops].sort((a, b) => a - b))
      expect(await orderBreaks(main, 'blocks'), 'блоки по разметке против экрана').toEqual([])
    })
  }
})
