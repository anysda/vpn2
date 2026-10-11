import type { Page } from '@playwright/test'
import { ADMIN_PASSWORD, ADMIN_USER, authPost, expect, hydrated, loginApi, loginUi, test, toast } from './support/fixtures'
import { totp } from './support/totp'

async function logoutUi(page: Page) {
  await page.getByRole('button', { name: `@${ADMIN_USER}` }).click()
  await page.getByRole('menuitem', { name: 'Выйти' }).click()
  await expect(page).toHaveURL(/\/login/)
  await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
}

/** Начать включение 2FA: библиотека просит текущий пароль. */
async function startSetup(page: Page) {
  await page.getByPlaceholder('Текущий пароль').fill(ADMIN_PASSWORD)
  await page.getByRole('button', { name: 'Включить 2FA' }).click()
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
    guard.allow(401, /\/api\/auth\/sign-in\/username$/, 'POST')
    await loginUi(page, `${ADMIN_PASSWORD}-wrong`)
    await expect(page.getByText('Неверный логин или пароль', { exact: true })).toBeVisible()
    await expect(page).toHaveURL(/\/login/)
    await loginUi(page)
    await expect(page).toHaveURL(/\/$/)
  })
})

test.describe('2FA', () => {
  // VPN2-69: короткий код фронт пропускает, сервер режет схемой zod. Человек
  // видел «Validation Error» (а бот в Telegram — дамп zod), теперь — поле по-русски.
  test('слишком короткий код при включении — понятная ошибка, а не дамп схемы', async ({ page, guard }) => {
    guard.allow(400, /\/api\/auth\/two-factor\/verify-totp$/, 'POST')
    await loginApi(page)
    await page.goto('/me')
    await hydrated(page)
    await startSetup(page)
    await page.getByLabel('Введи 6-значный код из приложения').fill('123')
    const resp = page.waitForResponse(r => new URL(r.url()).pathname === '/api/auth/two-factor/verify-totp')
    await page.getByRole('button', { name: 'Подтвердить' }).click()
    expect((await resp).status()).toBe(400)
    await expect(toast(page, 'Неверный код').getByText('Проверьте поле «Код»', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Отмена' }).click()
    await expect(page.getByRole('button', { name: 'Включить 2FA' })).toBeVisible()
  })

  test('неверный пароль при включении — понятная ошибка', async ({ page, guard }) => {
    guard.allow(400, /\/api\/auth\/two-factor\/enable$/, 'POST')
    await loginApi(page)
    await page.goto('/me')
    await hydrated(page)
    await page.getByPlaceholder('Текущий пароль').fill(`${ADMIN_PASSWORD}-wrong`)
    await page.getByRole('button', { name: 'Включить 2FA' }).click()
    await expect(toast(page, 'Ошибка setup').getByText('Неверный текущий пароль', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Включить 2FA' })).toBeVisible()
  })

  test('включить, войти с кодом, выключить; «Отмена» в настройке и в модалке', async ({ page, guard, request }) => {
    guard.allow(401, /\/api\/auth\/two-factor\/(verify-totp|disable)$/, 'POST')
    guard.allow(400, /\/api\/auth\/two-factor\/disable$/, 'POST')
    await loginApi(page)
    // Вторая сессия той же учётки: включение 2FA обязано её закрыть.
    const other = await authPost(request, '/sign-in/username', { username: ADMIN_USER, password: ADMIN_PASSWORD })
    expect(other.status(), await other.text()).toBe(200)
    await page.goto('/me')
    await hydrated(page)

    // «Отмена» на шаге с QR возвращает исходное состояние.
    await startSetup(page)
    await expect(page.getByLabel('Введи 6-значный код из приложения')).toBeVisible()
    await page.getByRole('button', { name: 'Отмена' }).click()
    await expect(page.getByRole('button', { name: 'Включить 2FA' })).toBeVisible()

    await startSetup(page)
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
      expect(await (await request.get('/api/auth/get-session')).json(), 'другая сессия пережила включение 2FA').toBeNull()

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
      await expect(page.getByText('Код не подошёл', { exact: true })).toBeVisible()
      const loginCode = await fresh()
      await code.fill(loginCode)
      await page.getByRole('button', { name: 'Подтвердить' }).click()
      await expect(page).toHaveURL(/\/$/)

      // VPN2-62: принятый код второй раз не пускает, даже с верным паролем и в окне шага.
      const first = await authPost(request, '/sign-in/username', { username: ADMIN_USER, password: ADMIN_PASSWORD })
      expect(await first.json()).toMatchObject({ twoFactorRedirect: true })
      const replay = await authPost(request, '/two-factor/verify-totp', { code: loginCode })
      expect(replay.status()).toBe(401)
      expect(await replay.json()).toMatchObject({ statusMessage: 'invalid_code', message: 'Код не подошёл' })

      // VPN2-69: повторная настройка при включённой 2FA (вторая вкладка) — был голый код.
      const again = await authPost(page.request, '/two-factor/enable', { password: ADMIN_PASSWORD })
      expect(again.status()).toBe(400)
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
      // и выключить; если уже выключена — 4xx, не беда.
      if (enabled) {
        await page.context().clearCookies()
        await authPost(page.request, '/sign-in/username', { username: ADMIN_USER, password: ADMIN_PASSWORD }).catch(() => {})
        await authPost(page.request, '/two-factor/verify-totp', { code: await fresh() }).catch(() => {})
        await authPost(page.request, '/two-factor/disable', { password: ADMIN_PASSWORD, code: await fresh() }).catch(() => {})
      }
    }
    // Вход снова без кода.
    await loginApi(page)
  })
})

test.describe('AdGuard под /adguard/', () => {
  test('без сессии панели уводит на вход, с сессией пускает', async ({ page }) => {
    const anon = await page.request.get('/adguard/', { headers: { accept: 'text/html' }, maxRedirects: 0 })
    expect(anon.status()).toBe(302)
    expect(anon.headers().location).toBe('/login')
    const api = await page.request.get('/adguard/control/status', { maxRedirects: 0 })
    expect(api.status()).toBe(401)

    await loginApi(page)
    const authed = await page.request.get('/adguard/', { headers: { accept: 'text/html' }, maxRedirects: 0 })
    // В e2e у AdGuard нет учётки: 503 значит, что запрос дошёл до прокси, а не
    // до страницы 404 или входа.
    expect(authed.status()).toBe(503)
    const authedApi = await page.request.get('/adguard/control/status', { maxRedirects: 0 })
    expect(authedApi.status()).toBe(503)
  })
})
