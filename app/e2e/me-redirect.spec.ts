import { expect, hydrated, loginApi, test } from './support/fixtures'

test('старый адрес /me ведёт в «Настройки» с запросом и якорем', async ({ page }) => {
  await loginApi(page)
  const res = await page.request.get('/me?from=bookmark', { maxRedirects: 0 })
  expect(res.status()).toBe(301)
  expect(res.headers().location).toBe('/settings?from=bookmark')

  await page.goto('/me?from=bookmark#tfa')
  await hydrated(page)
  await expect(page).toHaveURL(/\/settings\?from=bookmark#tfa$/)
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(['Настройки'])
})
