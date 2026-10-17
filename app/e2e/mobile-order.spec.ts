import { expect, test } from './support/fixtures'
import { openHome } from './support/clients'
import { orderBreaks } from './support/order'
import { TAB_PAGES } from '../app/utils/tab-pages'

// Телефон: Tab и скринридер идут по разметке, поэтому порядок в разметке — тот же,
// что на экране. Перестановка на экране (order, grid-row) без разметки — дефект.
test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

test('разделы таб-бара и /screen: фокусируемое по разметке идёт как на экране', async ({ page }) => {
  await openHome(page)
  const main = page.locator('main.tab.on')
  const breaks: Record<string, string[]> = {}
  for (const { path, label } of TAB_PAGES) {
    await page.locator('.mbar nav').getByRole('link', { name: label }).tap()
    await expect(page).toHaveURL(u => u.pathname === path)
    await expect(main).toBeVisible()
    breaks[path] = await orderBreaks(main, 'focusable')
  }
  // вкладки DNS — свои экраны в одном разделе
  await page.locator('.mbar nav').getByRole('link', { name: 'DNS' }).tap()
  await expect(page).toHaveURL(u => u.pathname === '/dns')
  for (const tab of ['Журнал', 'Фильтры', 'Свои правила', 'Перезаписи', 'Сервисы']) {
    const t = main.locator('.dseg').getByRole('tab', { name: tab, exact: true })
    await t.tap()
    await expect(t).toHaveAttribute('aria-selected', 'true')
    breaks[`/dns ${tab}`] = await orderBreaks(main, 'focusable')
  }
  await page.goto('/screen')
  await expect(main).toBeVisible()
  breaks['/screen'] = await orderBreaks(main, 'focusable')
  expect(breaks).toEqual(Object.fromEntries(Object.keys(breaks).map(k => [k, []])))
})
