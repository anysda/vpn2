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
    await expect(page.getByText('Неверный логин или пароль', { exact: true })).toBeVisible()
    await expect(page).toHaveURL(/\/login/)
    // Счётчик неудач на этот адрес сбрасывается успешным входом.
    await loginUi(page)
    await expect(page).toHaveURL(/\/$/)
  })
})

test.describe('2FA', () => {
  // VPN2-69: короткий код фронт пропускает, сервер режет схемой zod. Человек
  // видел «Validation Error» (а бот в Telegram — дамп zod), теперь — поле по-русски.
  test('слишком короткий код при включении — понятная ошибка, а не дамп схемы', async ({ page, guard }) => {
    guard.allow(400, /\/api\/auth\/totp\/confirm$/, 'POST')
    await loginApi(page)
    await page.goto('/me')
    await hydrated(page)
    await page.getByRole('button', { name: 'Включить 2FA' }).click()
    await page.getByLabel('Введи 6-значный код из приложения').fill('123')
    const resp = page.waitForResponse(r => new URL(r.url()).pathname === '/api/auth/totp/confirm')
    await page.getByRole('button', { name: 'Подтвердить' }).click()
    expect((await resp).status()).toBe(400)
    await expect(toast(page, 'Неверный код').getByText('Проверьте поле «Код»', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Отмена' }).click()
    await expect(page.getByRole('button', { name: 'Включить 2FA' })).toBeVisible()
  })

  test('включить, войти с кодом, выключить; «Отмена» в настройке и в модалке', async ({ page, guard, request }) => {
    guard.allow(401, /\/api\/auth\/login$/, 'POST')
    guard.allow(401, /\/api\/auth\/totp\/(confirm|disable)$/, 'POST')
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
    // Код, который сейчас точно неверен.
    const wrong = () => String((Number(totp(secret)) + 500_000) % 1_000_000).padStart(6, '0')
    // Панель гасит принятый шаг (VPN2-62): каждому подтверждению — код шага,
    // которого она ещё не видела. Сервер принимает шаг вперёд; дальше — ждать.
    let usedStep = 0
    let enabled = false
    const fresh = async () => {
      const now = Math.floor(Date.now() / 30_000)
      const step = Math.max(now, usedStep + 1)
      if (step > now + 1) await page.waitForTimeout((step - 1) * 30_000 - Date.now() + 500)
      usedStep = step
      return totp(secret, step * 30_000)
    }
    try {
      await expect(page.locator('svg').filter({ has: page.locator('rect') }).first()).toBeVisible()
      // VPN2-69: неверный код показывался голым «invalid_totp».
      await page.getByLabel('Введи 6-значный код из приложения').fill(wrong())
      await page.getByRole('button', { name: 'Подтвердить' }).click()
      await expect(toast(page, 'Неверный код').getByText('Код не подошёл', { exact: true })).toBeVisible()
      await page.getByLabel('Введи 6-значный код из приложения').fill(await fresh())
      await page.getByRole('button', { name: 'Подтвердить' }).click()
      enabled = true
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
      await code.fill(wrong())
      await page.getByRole('button', { name: 'Подтвердить' }).click()
      await expect(page.getByText('Неверный логин или пароль', { exact: true })).toBeVisible()
      const loginCode = await fresh()
      await code.fill(loginCode)
      await page.getByRole('button', { name: 'Подтвердить' }).click()
      await expect(page).toHaveURL(/\/$/)

      // VPN2-62: принятый код второй раз не пускает, даже с верным паролем и в окне шага.
      const replay = await request.post('/api/auth/login', {
        data: { username: ADMIN_USER, password: ADMIN_PASSWORD, totpCode: loginCode },
      })
      expect(replay.status()).toBe(401)
      expect(await replay.json()).toMatchObject({ statusMessage: 'invalid_credentials', message: 'Неверный логин или пароль' })

      // VPN2-69: повторная настройка при включённой 2FA (вторая вкладка) — был голый код.
      const again = await page.request.post('/api/auth/totp/setup')
      expect(again.status()).toBe(409)
      expect(await again.json()).toMatchObject({ statusMessage: 'totp_already_enabled', message: '2FA уже включена' })

      // Выключение: «Отмена» в модалке ничего не меняет, потом всерьёз.
      await page.goto('/me')
      await hydrated(page)
      await page.getByRole('button', { name: 'Выключить 2FA' }).click()
      const dlg = page.getByRole('dialog', { name: 'Выключить 2FA?' })
      await expect(dlg).toBeVisible()
      await dlg.getByRole('button', { name: 'Отмена' }).click()
      await expect(dlg).toBeHidden()
      await expect(page.getByText('Включена', { exact: true })).toBeVisible()

      // VPN2-69: ошибки модалки шли голыми кодами; тост виден поверх открытой модалки.
      await page.getByRole('button', { name: 'Выключить 2FA' }).click()
      await dlg.getByPlaceholder('Пароль').fill(`${ADMIN_PASSWORD}-wrong`)
      await dlg.getByPlaceholder('Код 2FA (6 цифр)').fill(totp(secret))
      await dlg.getByRole('button', { name: 'Выключить' }).click()
      await expect(toast(page, 'Ошибка').getByText('Неверный текущий пароль', { exact: true })).toBeVisible()
      await dlg.getByPlaceholder('Пароль').fill(ADMIN_PASSWORD)
      await dlg.getByPlaceholder('Код 2FA (6 цифр)').fill(wrong())
      await dlg.getByRole('button', { name: 'Выключить' }).click()
      await expect(toast(page, 'Код не подошёл').getByText('Ошибка', { exact: true })).toBeVisible()
      await dlg.getByPlaceholder('Код 2FA (6 цифр)').fill(await fresh())
      await dlg.getByRole('button', { name: 'Выключить' }).click()
      await expect(toast(page, '2FA выключена')).toBeVisible()
      enabled = false
      await expect(dlg).toBeHidden()
      await expect(page.getByText('Выключена', { exact: true })).toBeVisible()
    }
    finally {
      // Упавший посреди тест не должен оставить admin с 2FA: войти с кодом
      // (без 2FA код игнорируется) и выключить; если уже выключена — 4xx, не беда.
      if (enabled) {
        await page.request.post('/api/auth/login', {
          data: { username: ADMIN_USER, password: ADMIN_PASSWORD, totpCode: await fresh() },
        }).catch(() => {})
        await page.request.post('/api/auth/totp/disable', {
          data: { currentPassword: ADMIN_PASSWORD, totpCode: await fresh() },
        }).catch(() => {})
      }
    }
    // Вход снова без кода.
    await loginApi(page)
  })
})
