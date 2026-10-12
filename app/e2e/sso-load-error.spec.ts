import { expect, hydrated, loginApi, test } from './support/fixtures'

for (const width of [1400, 390]) {
  test.describe(`загрузка настроек входа, ${width}px`, () => {
    test.use({ viewport: { width, height: 844 } })

    test('ошибка GET видна, пустую форму нельзя сохранить, загрузку можно повторить', async ({ page, guard }) => {
      await loginApi(page)
      const original = await (await page.request.get('/api/admin/sso')).json()
      guard.allow(503, /\/api\/admin\/sso$/, 'GET')
      await page.route('**/api/admin/sso', async (route) => {
        if (route.request().method() !== 'GET') return route.continue()
        await route.fulfill({ status: 503, json: { message: 'Настройки временно недоступны' } })
      })
      await page.goto('/me')
      await hydrated(page)
      await expect(page.getByRole('button', { name: 'Сохранить', exact: true })).toBeDisabled()
      await expect(page.getByRole('alert').filter({ hasText: 'Настройки временно недоступны' })).toBeVisible()
      await page.unrouteAll({ behavior: 'wait' })
      await page.getByRole('button', { name: 'Повторить загрузку' }).click()
      await expect(page.getByRole('button', { name: 'Сохранить', exact: true })).toBeEnabled()
      await expect(page.getByLabel('Подпись кнопки')).toHaveValue(original.label)
      await expect(page.getByRole('alert').filter({ hasText: 'Настройки временно недоступны' })).toHaveCount(0)
    })
  })
}
