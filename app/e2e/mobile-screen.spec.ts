import type { Page } from '@playwright/test'
import { expect, test } from './support/fixtures'
import { openHome } from './support/clients'

// Телефон и «Настроить экран»: пункт меню, порядок блоков Обзора, фокус под шапкой.
test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

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
