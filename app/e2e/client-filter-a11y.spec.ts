import type { Page } from '@playwright/test'
import { expect, loginApi, test } from './support/fixtures'

/**
 * Сегменты фильтра клиентов - переключатели на одной странице. Скринридер
 * должен понять, какой фильтр выбран: выбранный сегмент нажат
 * (aria-pressed="true"), остальные нет, и ни один не объявлен
 * «текущей страницей» (aria-current), раз путь у всех один.
 */

const segments = (page: Page) => page.locator('.seg [role="button"]')
const segment = (page: Page, name: string) => page.getByRole('button', { name: new RegExp(`^${name}`) })

async function expectOnlyPressed(page: Page, name: string) {
  await expect(segments(page)).toHaveCount(3)
  await expect(page.locator('.seg [role="button"][aria-pressed="true"]')).toHaveCount(1)
  await expect(segment(page, name)).toHaveAttribute('aria-pressed', 'true')
  await expect(page.locator('.seg [role="button"][aria-pressed="false"]')).toHaveCount(2)
  await expect(page.locator('.seg [aria-current]')).toHaveCount(0)
}

test('на /clients?f=exp нажат только «Истекают», «текущей страницы» среди сегментов нет', async ({ page }) => {
  await loginApi(page)
  await page.goto('/clients?f=exp')
  await expectOnlyPressed(page, 'Истекают')
})

test.describe('разметка сервера', () => {
  test.use({ javaScriptEnabled: false })

  test('без JS: на /clients?f=exp нажат только «Истекают», «текущей страницы» нет', async ({ page }) => {
    await loginApi(page)
    await page.goto('/clients?f=exp')
    await expectOnlyPressed(page, 'Истекают')
  })
})

test('после клика «Заморожены» нажат только он, «текущей страницы» среди сегментов нет', async ({ page }) => {
  await loginApi(page)
  await page.goto('/clients?f=exp')
  await expectOnlyPressed(page, 'Истекают')
  await segment(page, 'Заморожены').click()
  await expect(page).toHaveURL(/[?&]f=frz\b/)
  await expectOnlyPressed(page, 'Заморожены')
})
