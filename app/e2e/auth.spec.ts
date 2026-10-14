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
  await page.locator('#tfa').getByLabel('Текущий пароль').fill(ADMIN_PASSWORD)
  await page.getByRole('button', { name: 'Включить 2FA' }).click()
}

test.describe('вход', () => {
  test('выход отменяет незавершённый опрос до удаления сессии', async ({ page }) => {
    await page.addInitScript(() => {
      // Проверяем один опрос, без отмены следующим интервалом (dedupe).
      window.setInterval = () => 0
      const original = window.fetch
      let signal: AbortSignal | null | undefined
      window.fetch = (input, init) => {
        const url = new URL(input instanceof Request ? input.url : String(input), location.href)
        if (url.pathname === '/api/ops/nodes') signal = init?.signal ?? (input instanceof Request ? input.signal : undefined)
        if (url.pathname === '/api/auth/sign-out') Reflect.set(window, '__pollCancelledBeforeLogout', signal?.aborted ?? false)
        return original(input, init)
      }
    })
    await loginApi(page)
    await page.goto('/')
    await hydrated(page)
    let entered!: () => void
    let release!: () => void
    const pending = new Promise<void>(resolve => { entered = resolve })
    const held = new Promise<void>(resolve => { release = resolve })
    await page.route('**/api/ops/nodes', async (route) => {
      entered()
      await held
      await route.fulfill({ status: 401, json: {} }).catch(() => {})
    })
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
    await pending
    try {
      await logoutUi(page)
      const cancelled = await page.evaluate(() => Reflect.get(window, '__pollCancelledBeforeLogout'))
      console.log(`Опрос отменён до sign-out: ${cancelled}`)
      expect(cancelled).toBe(true)
    }
    finally { release() }
  })

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
    await page.goto('/settings')
    await hydrated(page)
    await startSetup(page)
    // Не 6 цифр - отказ на клиенте, как в макете.
    await page.getByLabel('Введи 6-значный код из приложения').fill('123')
    await page.getByRole('button', { name: 'Подтвердить' }).click()
    await expect(toast(page, 'Код не подошёл')).toBeVisible()
    // Сервер в обход формы отвечает полем по-русски.
    const resp = await authPost(page.request, '/two-factor/verify-totp', { code: '123' })
    expect(resp.status()).toBe(400)
    expect(JSON.stringify(await resp.json())).toContain('Проверьте поле «Код»')
    await page.getByRole('button', { name: 'Отмена' }).click()
    await expect(page.getByRole('button', { name: 'Включить 2FA' })).toBeVisible()
  })

  test('неверный пароль при включении — понятная ошибка', async ({ page, guard }) => {
    guard.allow(400, /\/api\/auth\/two-factor\/enable$/, 'POST')
    await loginApi(page)
    await page.goto('/settings')
    await hydrated(page)
    await page.locator('#tfa').getByLabel('Текущий пароль').fill(`${ADMIN_PASSWORD}-wrong`)
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
    await page.goto('/settings')
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
      await expect(page.locator('form').getByText('Код не подошёл', { exact: true })).toBeVisible()
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
      await page.goto('/settings')
      await hydrated(page)
      await page.getByRole('button', { name: 'Выключить 2FA' }).click()
      const dlg = page.getByRole('dialog', { name: 'Выключить 2FA?' })
      await expect(dlg).toBeVisible()
      await dlg.getByRole('button', { name: 'Отмена' }).click()
      await expect(dlg).toBeHidden()
      await expect(page.getByText('Включена', { exact: true })).toBeVisible()

      // VPN2-69: ошибки модалки шли голыми кодами; тост виден поверх открытой модалки.
      await page.getByRole('button', { name: 'Выключить 2FA' }).click()
      await dlg.getByLabel('Пароль', { exact: true }).fill(`${ADMIN_PASSWORD}-wrong`)
      await dlg.getByLabel('Код 2FA (6 цифр)').fill(totp(secret))
      await dlg.getByRole('button', { name: 'Выключить' }).click()
      await expect(toast(page, 'Ошибка').getByText('Неверный текущий пароль', { exact: true })).toBeVisible()
      await dlg.getByLabel('Пароль', { exact: true }).fill(ADMIN_PASSWORD)
      await dlg.getByLabel('Код 2FA (6 цифр)').fill(wrong())
      await dlg.getByRole('button', { name: 'Выключить' }).click()
      await expect(toast(page, 'Код не подошёл').getByText('Ошибка', { exact: true })).toBeVisible()
      await dlg.getByLabel('Код 2FA (6 цифр)').fill(await fresh())
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

test('родного интерфейса AdGuard в панели нет: /adguard/ отвечает как несуществующий адрес', async ({ page }) => {
  const paths = ['/adguard/', '/adguard/control/status', '/adguard/login.html']
  // Без сессии любая страница ведёт на вход — как и выдуманный адрес.
  for (const path of [...paths, '/no-such-page/']) {
    const anon = await page.request.get(path, { headers: { accept: 'text/html' }, maxRedirects: 0 })
    expect([anon.status(), anon.headers().location], path).toEqual([302, '/login'])
  }
  await loginApi(page)
  for (const path of paths) {
    const authed = await page.request.get(path, { headers: { accept: 'text/html' }, maxRedirects: 0 })
    expect(authed.status(), path).toBe(404)
  }
  const api = await page.request.post('/adguard/control/filtering/set_rules', { data: { rules: [] }, maxRedirects: 0 })
  expect(api.status()).toBe(404)
})
