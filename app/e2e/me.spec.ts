import { ADMIN_PASSWORD, ADMIN_USER, expect, hydrated, loginApi, loginUi, test, toast } from './support/fixtures'

test.describe('страница «Я»', () => {
  test('логин виден, смена пароля проверяет поля, меняет и возвращает пароль', async ({ page }) => {
    await loginApi(page)
    await page.goto('/')
    await hydrated(page)
    await page.getByRole('button', { name: `@${ADMIN_USER}` }).click()
    await page.getByRole('menuitem', { name: 'Мой профиль' }).click()
    await expect(page).toHaveURL(/\/me$/)
    await expect(page.getByRole('heading', { name: 'Мой профиль' })).toBeVisible()
    await expect(page.getByText(ADMIN_USER, { exact: true })).toBeVisible()
    await expect(page.getByText(/Включена|Выключена/)).toBeVisible()

    const cur = page.getByLabel('Текущий пароль')
    const next = page.getByLabel('Новый пароль (≥ 8 символов)')
    const again = page.getByLabel('Повторите')
    const save = page.getByRole('button', { name: 'Сохранить пароль' })

    // Не совпадают — кнопка неактивна.
    await cur.fill(ADMIN_PASSWORD)
    await next.fill('abcdefgh1')
    await again.fill('abcdefgh2')
    await expect(save).toBeDisabled()
    // Короткий — отказ на клиенте, запроса нет.
    await next.fill('short')
    await again.fill('short')
    await save.click()
    await expect(toast(page, 'Пароль должен быть ≥ 8 символов')).toBeVisible()

    const temp = `${ADMIN_PASSWORD}-e2e`
    let changed = false
    try {
      await next.fill(temp)
      await again.fill(temp)
      await save.click()
      await expect(toast(page, 'Пароль обновлён')).toBeVisible()
      changed = true
      await expect(cur).toHaveValue('')

      // Новый пароль действительно работает для входа.
      await page.context().clearCookies()
      await loginUi(page, temp)
      await expect(page).toHaveURL(/\/$/)

      await page.goto('/me')
      await hydrated(page)
      await cur.fill(temp)
      await next.fill(ADMIN_PASSWORD)
      await again.fill(ADMIN_PASSWORD)
      await save.click()
      await expect(toast(page, 'Пароль обновлён')).toBeVisible()
      changed = false
    }
    finally {
      if (changed) {
        // Вернуть пароль admin, даже если тест упал посередине.
        await page.request.post('/api/auth/login', { data: { username: ADMIN_USER, password: temp } }).catch(() => {})
        await page.request.post('/api/auth/password', {
          data: { currentPassword: temp, newPassword: ADMIN_PASSWORD },
        }).catch(() => {})
      }
    }
    await page.context().clearCookies()
    await loginApi(page)
  })
})
