import type { Page } from '@playwright/test'
import { ADMIN_PASSWORD, ADMIN_USER, expect, hydrated, loginApi, loginUi, test, toast } from './support/fixtures'
import { totp } from './support/totp'

async function logoutUi(page: Page) {
  await page.getByRole('button', { name: `@${ADMIN_USER}` }).click()
  await page.getByRole('menuitem', { name: 'Выйти' }).click()
  await expect(page).toHaveURL(/\/login/)
  await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
}

test.describe('вход', () => {
  test('паролем, затем выход', async ({ page }) => {
    await loginUi(page)
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByRole('button', { name: `@${ADMIN_USER}` })).toBeVisible()
    await logoutUi(page)
    // Сессии больше нет: главная уводит на вход.
    await page.goto('/')
    await expect(page).toHaveURL(/\/login/)
  })

  test('неверный пароль показывает ошибку и не пускает', async ({ page, guard }) => {
    guard.allow(401, /\/api\/auth\/login$/, 'POST')
    await loginUi(page, `${ADMIN_PASSWORD}-wrong`)
    await expect(page.getByText('invalid_credentials')).toBeVisible()
    await expect(page).toHaveURL(/\/login/)
    // Счётчик неудач на этот адрес сбрасывается успешным входом.
    await loginUi(page)
    await expect(page).toHaveURL(/\/$/)
  })
})

test.describe('2FA', () => {
  test('включить, войти с кодом, выключить; «Отмена» в настройке и в модалке', async ({ page, guard }) => {
    guard.allow(401, /\/api\/auth\/login$/, 'POST')
    await loginApi(page)
    await page.goto('/me')
    await hydrated(page)

    // «Отмена» на шаге с QR возвращает исходное состояние.
    await page.getByRole('button', { name: 'Включить 2FA' }).click()
    await expect(page.getByLabel('Введи 6-значный код из приложения')).toBeVisible()
    await page.getByRole('button', { name: 'Отмена' }).click()
    await expect(page.getByRole('button', { name: 'Включить 2FA' })).toBeVisible()

    await page.getByRole('button', { name: 'Включить 2FA' }).click()
    const secretInput = page.locator('input[readonly]')
    await expect(secretInput).toHaveValue(/^[A-Z2-7]{16,}$/)
    const secret = await secretInput.inputValue()
    try {
      await expect(page.locator('svg').filter({ has: page.locator('rect') }).first()).toBeVisible()
      await page.getByLabel('Введи 6-значный код из приложения').fill(totp(secret))
      await page.getByRole('button', { name: 'Подтвердить' }).click()
      await expect(toast(page, '2FA включена')).toBeVisible()
      await expect(page.getByText('Включена', { exact: true })).toBeVisible()

      // Выход и вход: после пароля спрашивают код.
      await logoutUi(page)
      await page.getByLabel('Логин').fill(ADMIN_USER)
      await page.getByLabel('Пароль').fill(ADMIN_PASSWORD)
      await page.getByRole('button', { name: 'Войти' }).click()
      const code = page.getByPlaceholder('123 456')
      await expect(code).toBeVisible()
      // Неверный код — ошибка, форма остаётся на шаге кода.
      const bad = String((Number(totp(secret)) + 500_000) % 1_000_000).padStart(6, '0')
      await code.fill(bad)
      await page.getByRole('button', { name: 'Подтвердить' }).click()
      await expect(page.getByText('invalid_credentials')).toBeVisible()
      await code.fill(totp(secret))
      await page.getByRole('button', { name: 'Подтвердить' }).click()
      await expect(page).toHaveURL(/\/$/)

      // Выключение: «Отмена» в модалке ничего не меняет, потом всерьёз.
      await page.goto('/me')
      await hydrated(page)
      await page.getByRole('button', { name: 'Выключить 2FA' }).click()
      const dlg = page.getByRole('dialog', { name: 'Выключить 2FA?' })
      await expect(dlg).toBeVisible()
      await dlg.getByRole('button', { name: 'Отмена' }).click()
      await expect(dlg).toBeHidden()
      await expect(page.getByText('Включена', { exact: true })).toBeVisible()

      await page.getByRole('button', { name: 'Выключить 2FA' }).click()
      await dlg.getByPlaceholder('Пароль').fill(ADMIN_PASSWORD)
      await dlg.getByPlaceholder('Код 2FA (6 цифр)').fill(totp(secret))
      await dlg.getByRole('button', { name: 'Выключить' }).click()
      await expect(toast(page, '2FA выключена')).toBeVisible()
      await expect(dlg).toBeHidden()
      await expect(page.getByText('Выключена', { exact: true })).toBeVisible()
    }
    finally {
      // Упавший посреди тест не должен оставить admin с 2FA: войти с кодом
      // (без 2FA код игнорируется) и выключить; если уже выключена — 4xx, не беда.
      await page.request.post('/api/auth/login', {
        data: { username: ADMIN_USER, password: ADMIN_PASSWORD, totpCode: totp(secret) },
      }).catch(() => {})
      await page.request.post('/api/auth/totp/disable', {
        data: { currentPassword: ADMIN_PASSWORD, totpCode: totp(secret) },
      }).catch(() => {})
    }
    // Вход снова без кода.
    await loginApi(page)
  })
})
