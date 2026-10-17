import type { Page } from '@playwright/test'
import { expect, test } from './support/fixtures'
import { openHome } from './support/clients'

// Safari на iPhone увеличивает страницу при фокусе в поле со шрифтом меньше 16px.
// Правило одно на всю панель: проверяются все поля каждого раздела, в том числе
// подразделы DNS и окно «Новый клиент».

const DNS_SUBS = ['Сводка', 'Журнал', 'Фильтры', 'Свои правила', 'Перезаписи', 'Сервисы', 'Клиенты', 'Настройки DNS']

/** Поля страницы и их шрифт; скрытые тоже — у них тот же computed font-size. */
function fields(page: Page) {
  return page.evaluate(() => [...document.querySelectorAll<HTMLElement>('input, select, textarea')].map(el => ({
    size: Number.parseFloat(getComputedStyle(el).fontSize),
    what: `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} «${el.getAttribute('aria-label') ?? el.getAttribute('placeholder') ?? el.getAttribute('name') ?? ''}»`,
  })))
}

async function allAtLeast16(page: Page, where: string) {
  const list = await fields(page)
  expect(list.length, `${where}: поля есть`).toBeGreaterThan(0)
  expect(list.filter(f => f.size < 16).map(f => `${f.what} ${f.size}px`), `${where}: поля мельче 16px`).toEqual([])
}

async function noZoomOnFocus(page: Page, where: string) {
  const field = page.locator('input:visible, textarea:visible, select:visible').first()
  await field.tap()
  expect(await page.evaluate(() => window.visualViewport?.scale ?? 1), `${where}: масштаб после фокуса`).toBe(1)
}

for (const width of [360, 402]) {
  test.describe(`поля на телефоне ${width}px`, () => {
    test.use({ viewport: { width, height: 874 }, isMobile: true, hasTouch: true })

    test('шрифт полей во всех разделах не меньше 16px, страница не увеличивается', async ({ page }) => {
      await openHome(page, '/clients')
      await allAtLeast16(page, '/clients')
      await noZoomOnFocus(page, '/clients')
      await page.getByRole('button', { name: 'Новый клиент' }).tap()
      await expect(page.getByRole('dialog', { name: 'Новый клиент' })).toBeVisible()
      await allAtLeast16(page, '/clients, Новый клиент')

      for (const path of ['/routes', '/settings']) {
        await openHome(page, path)
        await allAtLeast16(page, path)
        await noZoomOnFocus(page, path)
      }

      await openHome(page, '/dns')
      const seg = page.locator('.dns .dseg')
      for (const name of DNS_SUBS) {
        const tab = seg.getByRole('tab', { name, exact: true })
        await tab.tap()
        await expect(tab).toHaveAttribute('aria-selected', 'true')
      }
      await allAtLeast16(page, '/dns, все подразделы')
    })
  })
}
