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
      await page.goto('/settings')
      await hydrated(page)
      // В «Настройках» есть и форма ботов со своей «Сохранить».
      const sso = page.locator('form').filter({ hasText: 'Кого пускать' })
      await expect(sso.getByRole('button', { name: 'Сохранить', exact: true })).toBeDisabled()
      await expect(page.getByRole('alert').filter({ hasText: 'Настройки временно недоступны' })).toBeVisible()
      await page.unrouteAll({ behavior: 'wait' })
      await page.getByRole('button', { name: 'Повторить загрузку' }).click()
      await expect(page.getByLabel('Подпись кнопки')).toHaveValue(original.label)
      // Загруженная форма доступна; «Сохранить» оживает с первой правкой.
      await expect(sso.getByRole('button', { name: 'Сохранить', exact: true })).toBeDisabled()
      await page.getByLabel('Подпись кнопки').fill(`${original.label} e2e`)
      await expect(sso.getByRole('button', { name: 'Сохранить', exact: true })).toBeEnabled()
      await expect(page.getByRole('alert').filter({ hasText: 'Настройки временно недоступны' })).toHaveCount(0)
    })
  })
}
