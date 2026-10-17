import type { Page } from '@playwright/test'
import { expect, loginApi, test, uniq } from './support/fixtures'
import { clientCard, clientName, openHome } from './support/clients'

// Телефон: порядок шапки в разметке совпадает с экраном — Tab и скринридер
// идут слева направо, «Назад» карточки клиента первым.
test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

/** Видимые элементы шапки в порядке разметки — слева направо и на экране. */
async function headerInDomOrder(page: Page, where: string) {
  const xs = await page.locator('.wrap>header').evaluate(h => [...h.children]
    .map(el => ({ el: el.className || el.tagName, box: el.getBoundingClientRect() }))
    .filter(c => c.box.width > 0 && c.box.height > 0)
    .map(c => ({ el: c.el, x: Math.round(c.box.left) })))
  expect(xs.length, where).toBeGreaterThan(0)
  expect(xs, `${where}: разметка шапки не в порядке экрана`).toEqual([...xs].sort((a, b) => a.x - b.x))
}

test('карточка клиента: «Назад» первый в шапке и первый по Tab', async ({ page }) => {
  await loginApi(page)
  const name = uniq('hdr')
  expect((await page.request.post('/api/clients', { data: { name } })).status()).toBe(200)
  await openHome(page, '/clients')
  await headerInDomOrder(page, 'список')
  await page.getByPlaceholder('Поиск...').fill(name)
  await clientCard(page, name).tap()
  await expect(clientName(page.getByTestId('client-detail'))).toHaveText(name)

  const back = page.getByRole('button', { name: 'Назад' })
  await expect(back).toBeVisible()
  await headerInDomOrder(page, 'карточка клиента')
  const first = await page.locator('.wrap>header>*').first().getAttribute('aria-label')
  expect(first).toBe('Назад')

  // Tab с самой шапки: первый фокус — первый элемент разметки.
  await page.locator('.wrap>header').evaluate((h: HTMLElement) => { h.tabIndex = -1; h.focus() })
  await page.keyboard.press('Tab')
  await expect(back).toBeFocused()
})

for (const url of ['/', '/routes', '/dns', '/settings']) {
  test(`${url}: шапка в порядке экрана`, async ({ page }) => {
    await openHome(page, url)
    await headerInDomOrder(page, url)
  })
}
