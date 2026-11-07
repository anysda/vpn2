import { expect, hydrated, loginApi, test } from './support/fixtures'

// Cookie сессии исчезла на живой странице (очистили, вышли в другой вкладке):
// опросы панели не уходят без сессии и не сыплют 401 в консоль.
// Сторож из fixtures ловит любой 401 за тест.

test('cookie стёрты на странице настроек: опросы админки не уходят без сессии', async ({ page }) => {
  await loginApi(page)
  await page.goto('/settings')
  await hydrated(page)
  // Возврат на вкладку сразу обновляет статус бота: тот же запрос, что опрос раз в 10 с.
  const refreshed = page.waitForResponse(r => new URL(r.url()).pathname === '/api/admin/telegram')
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  expect((await refreshed).status(), 'положительный контроль: с сессией статус бота приходит').toBe(200)

  const sent: string[] = []
  page.on('request', (r) => {
    const path = new URL(r.url()).pathname
    if (path.startsWith('/api/') && !path.startsWith('/api/auth/')) sent.push(`${r.method()} ${path}`)
  })
  await page.context().clearCookies()
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  // Опрос доставок ботов идёт раз в 2 с: окно его захватывает.
  await page.waitForTimeout(2_500)
  expect(sent, 'запросы без сессии').toEqual([])
})
