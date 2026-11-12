import { type Page, type Locator, test as baseTest } from '@playwright/test'
import { ADMIN_PASSWORD, ADMIN_USER, authPost, expect, hydrated, loginApi, loginUi, toast, Guard, test } from './support/fixtures'
import { totp } from './support/totp'
import { noHorizontalOverflow } from './support/overflow'

test.describe('auth', () => {
  async function logoutUi(page: Page) {
    await page.getByRole('button', { name: `@${ADMIN_USER}` }).click()
    await page.getByRole('menuitem', { name: 'Выйти' }).click()
    await expect(page).toHaveURL(/\/login/)
    await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
  }

  /**
   * Секрет 2FA, включённой тестом, и последний принятый панелью шаг кода.
   * Панель гасит принятый шаг (VPN2-62): каждому подтверждению — код шага,
   * которого она ещё не видела. Сервер принимает шаг вперёд; дальше — ждать.
   */
  interface Totp { secret: string, usedStep: number }

  async function freshCode(state: Totp) {
    const now = Math.floor(Date.now() / 30_000)
    const step = Math.max(now, state.usedStep + 1)
    // Ждём таймером, а не страницей: уборка идёт и тогда, когда страница закрыта.
    if (step > now + 1) await new Promise(resolve => setTimeout(resolve, (step - 1) * 30_000 - Date.now() + 500))
    state.usedStep = step
    return totp(state.secret, step * 30_000)
  }

  /**
   * Уборка 2FA admin после теста, в том числе упавшего или снятого по таймауту.
   * В finally теста она не годилась: по таймауту контекст страницы уже закрыт,
   * уборка падала на clearCookies, а оставленная 2FA валила вход всем следующим
   * тестам шарда. Здесь свой API-контекст, от браузера не зависит.
   */
  const tfaTest = test.extend<{ tfa: Totp }>({
    tfa: async ({ playwright, baseURL }, use) => {
      const state: Totp = { secret: '', usedStep: 0 }
      await use(state)
      if (!state.secret) return
      const api = await playwright.request.newContext({ baseURL, extraHTTPHeaders: { origin: new URL(baseURL!).origin } })
      try {
        const login = await authPost(api, '/sign-in/username', { username: ADMIN_USER, password: ADMIN_PASSWORD })
        expect(login.status(), await login.text()).toBe(200)
        // Тест дошёл до выключения сам или не успел включить.
        if (!(await login.json()).twoFactorRedirect) return
        const verify = await authPost(api, '/two-factor/verify-totp', { code: await freshCode(state) })
        expect(verify.status(), await verify.text()).toBe(200)
        const disable = await authPost(api, '/two-factor/disable', { password: ADMIN_PASSWORD, code: await freshCode(state) })
        expect(disable.status(), await disable.text()).toBe(200)
      }
      finally {
        await api.dispose()
      }
    },
  })

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

    tfaTest('включить, войти с кодом, выключить; «Отмена» в настройке и в модалке', async ({ page, guard, request, tfa }) => {
      // Зависшее действие падает само, а не съедает весь тест: на уборку остаётся время.
      page.setDefaultTimeout(20_000)
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
      tfa.secret = secret
      // Код, который сейчас точно неверен.
      const wrong = () => String((Number(totp(secret)) + 500_000) % 1_000_000).padStart(6, '0')
      const fresh = () => freshCode(tfa)
      await expect(page.locator('svg').filter({ has: page.locator('rect') }).first()).toBeVisible()
      // VPN2-69: неверный код показывался голым «invalid_totp».
      await page.getByLabel('Введи 6-значный код из приложения').fill(wrong())
      await page.getByRole('button', { name: 'Подтвердить' }).click()
      await expect(toast(page, 'Неверный код').getByText('Код не подошёл', { exact: true })).toBeVisible()
      await page.getByLabel('Введи 6-значный код из приложения').fill(await fresh())
      await page.getByRole('button', { name: 'Подтвердить' }).click()
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
      await expect(dlg).toBeHidden()
      await expect(page.getByText('Выключена', { exact: true })).toBeVisible()
      // Вход снова без кода.
      await loginApi(page)
    })
  })

  test.describe('2FA: уборка после теста, упавшего на закрытом контексте', () => {
    tfaTest('тест с включённой 2FA падает, когда контекст страницы уже закрыт', async ({ page, tfa }) => {
      // Ожидаемый провал, как у 2FA на CI: страница закрыта раньше, чем тест выключил 2FA.
      tfaTest.fail()
      await loginApi(page)
      const setup = await authPost(page.request, '/two-factor/enable', { password: ADMIN_PASSWORD })
      expect(setup.status(), await setup.text()).toBe(200)
      tfa.secret = new URL((await setup.json()).totpURI).searchParams.get('secret')!
      const enable = await authPost(page.request, '/two-factor/verify-totp', { code: await freshCode(tfa) })
      expect(enable.status(), await enable.text()).toBe(200)
      await page.context().close()
      await page.goto('/settings')
    })

    test('следующий тест входит без кода', async ({ page }) => {
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
})

test.describe('logout-guard', () => {
  const test = baseTest

  for (const path of ['/api/admin/telegram', '/api/ops/nodes', '/api/ops/nodes?refresh=1']) {
    test(`сторож: отложенный ${path} 401 после успешного выхода ожидаем`, async ({ page }) => {
      const guard = new Guard()
      guard.attach(page)
      await page.route('**/*', route => route.fulfill({
        status: route.request().url().endsWith(path) ? 401 : 200,
        contentType: 'application/json',
        body: '{}',
      }))
      await page.goto('/')
      await page.evaluate(async (path) => {
        await fetch('/api/auth/sign-out', { method: 'POST' })
        await fetch(path)
      }, path)
      expect(guard.problems).toEqual([])
    })
  }

  test('сторож: 401 до выхода, после неудачного выхода и после нового входа остаются ошибками', async ({ page }) => {
    const guard = new Guard()
    guard.attach(page)
    let logoutStatus = 500
    await page.route('**/*', route => route.fulfill({
      status: route.request().url().endsWith('/api/auth/sign-out') ? logoutStatus
        : route.request().url().endsWith('/api/admin/telegram') ? 401 : 200,
      contentType: 'application/json',
      body: '{}',
    }))
    await page.goto('/')
    await page.evaluate(async () => {
      await fetch('/api/admin/telegram')
      await fetch('/api/auth/sign-out', { method: 'POST' })
      await fetch('/api/admin/telegram')
    })
    logoutStatus = 200
    await page.evaluate(async () => {
      await fetch('/api/auth/sign-out', { method: 'POST' })
      await fetch('/api/auth/sign-in/username', { method: 'POST' })
      await fetch('/api/admin/telegram')
    })
    expect(guard.problems.filter(p => p.startsWith('http 401 GET'))).toHaveLength(3)
  })

  test('сторож: после выхода посторонние ошибки и запись в telegram не скрываются', async ({ page }) => {
    const guard = new Guard()
    guard.attach(page)
    await page.route('**/*', (route) => {
      const path = new URL(route.request().url()).pathname
      return route.fulfill({ status: path === '/api/auth/sign-out' || path === '/' ? 200 : 401, body: '{}' })
    })
    await page.goto('/')
    await page.evaluate(async () => {
      await fetch('/api/auth/sign-out', { method: 'POST' })
      await fetch('/api/admin/telegram', { method: 'PUT' })
      await fetch('/api/clients')
    })
    expect(guard.problems.filter(p => p.startsWith('http 401'))).toHaveLength(2)
  })

  for (const { status, path } of [
    { status: 500, path: '/api/admin/telegram' },
    { status: 401, path: '/api/admin/telegram/settings' },
  ]) {
    test(`сторож: после выхода GET ${path} со статусом ${status} остаётся ошибкой`, async ({ page }) => {
      const guard = new Guard()
      guard.attach(page)
      await page.route('**/*', route => route.fulfill({
        status: new URL(route.request().url()).pathname === path ? status : 200,
        contentType: 'application/json',
        body: '{}',
      }))
      await page.goto('/')
      await page.evaluate(async (path) => {
        await fetch('/api/auth/sign-out', { method: 'POST' })
        await fetch(path)
      }, path)
      // Проверяем конкретный ответ: посторонняя ошибка не должна озеленять тест.
      await expect.poll(() => guard.problems.filter(p => p.startsWith('http '))).toEqual([
        `http ${status} GET ${new URL(path, page.url()).href}`,
      ])
    })
  }
})

test.describe('me', () => {
  test.describe('страница «Я»', () => {
    test('логин виден, смена пароля проверяет поля, меняет и возвращает пароль', async ({ page, guard, request }) => {
      guard.allow(400, /\/api\/auth\/change-password$/, 'POST')
      await loginApi(page)
      // Вторая сессия той же учётки: смена пароля обязана её закрыть.
      const other = await authPost(request, '/sign-in/username', { username: ADMIN_USER, password: ADMIN_PASSWORD })
      expect(other.status(), await other.text()).toBe(200)
      await page.goto('/')
      await hydrated(page)
      await page.getByRole('button', { name: `@${ADMIN_USER}` }).click()
      await page.getByRole('menuitem', { name: 'Мой профиль' }).click()
      await expect(page).toHaveURL(/\/settings$/)
      // VPN2-337: заголовок страницы один - «Настройки», профиль стал карточкой.
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(['Настройки'])
      await expect(page.getByRole('heading', { name: 'Мой профиль' })).toBeVisible()
      await expect(page.getByText(ADMIN_USER, { exact: true })).toBeVisible()
      await expect(page.getByText(/Включена|Выключена/)).toBeVisible()

      // «Текущий пароль» есть и в блоке 2FA - берём форму смены пароля.
      const form = page.locator('form').filter({ has: page.getByRole('button', { name: 'Сохранить пароль' }) })
      const cur = form.getByLabel('Текущий пароль')
      const next = form.getByLabel('Новый пароль')
      const again = form.getByLabel('Повторите')
      const save = form.getByRole('button', { name: 'Сохранить пароль' })

      // Пустой текущий - отказ на клиенте.
      await save.click()
      await expect(toast(page, 'Введите текущий пароль')).toBeVisible()
      // Не совпадают - отказ на клиенте.
      await cur.fill(ADMIN_PASSWORD)
      await next.fill('abcdefgh1')
      await again.fill('abcdefgh2')
      await save.click()
      await expect(toast(page, 'Пароли не совпадают')).toBeVisible()
      // Короткий — отказ на клиенте, запроса нет.
      await next.fill('short')
      await again.fill('short')
      await save.click()
      await expect(toast(page, 'Пароль должен быть ≥ 8 символов')).toBeVisible()

      const temp = `${ADMIN_PASSWORD}-e2e`
      // VPN2-69: неверный текущий пароль показывался голым «invalid_current_password».
      await cur.fill(`${ADMIN_PASSWORD}-wrong`)
      await next.fill(temp)
      await again.fill(temp)
      await save.click()
      await expect(toast(page, 'Ошибка').getByText('Неверный текущий пароль', { exact: true })).toBeVisible()
      await cur.fill(ADMIN_PASSWORD)

      let changed = false
      try {
        await next.fill(temp)
        await again.fill(temp)
        await save.click()
        await expect(toast(page, 'Пароль обновлён')).toBeVisible()
        changed = true
        await expect(cur).toHaveValue('')
        const otherSession = await (await request.get('/api/auth/get-session')).json()
        expect(otherSession, 'другая сессия пережила смену пароля').toBeNull()

        // Новый пароль действительно работает для входа.
        await page.context().clearCookies()
        await loginUi(page, temp)
        await expect(page).toHaveURL(/\/$/)

        await page.goto('/settings')
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
          await authPost(page.request, '/sign-in/username', { username: ADMIN_USER, password: temp }).catch(() => {})
          await authPost(page.request, '/change-password', { currentPassword: temp, newPassword: ADMIN_PASSWORD }).catch(() => {})
        }
      }
      await page.context().clearCookies()
      await loginApi(page)
    })
  })

  test.describe('вход через Authentik из профиля', () => {
    const base = {
      enabled: true,
      authentikUrl: 'https://auth.example.invalid',
      appSlug: 'vpn2-e2e',
      clientId: 'vpn2-e2e',
      clientSecret: 'e2e-client-secret-0123456789',
      allowedSubs: '00000000-0000-4000-8000-000000000001',
      label: 'E2E IdP',
      autoRedirect: false,
      passwordLogin: true,
      redirectUrl: 'https://vpn2.example.invalid/auth/authentik',
    }

    test('форма не даёт запереть панель, секрет целиком не отдаётся, вход меняется без передеплоя', async ({ page, guard, browser }) => {
      guard.allow(400, /\/api\/admin\/sso(\/check)?$/)
      await loginApi(page)
      try {
        await page.goto('/settings')
        await hydrated(page)
        const card = page.locator('form').filter({ hasText: 'Кого пускать' })
        await expect(card).toBeVisible()

        // Пустой «кого пускать» при включённом SSO — не войдёт никто.
        await card.getByLabel('Вход через Authentik').click()
        await card.getByLabel('Адрес Authentik или discovery URL').fill(base.authentikUrl)
        await card.getByLabel('client_id').fill(base.clientId)
        await card.getByLabel('client_secret').fill(base.clientSecret)
        await card.getByLabel('Кого пускать').fill('')
        // Подставленный адрес возврата — от адреса стенда, а он по HTTP.
        await expect(card.getByLabel('Адрес возврата')).toHaveValue(/\/auth\/authentik$/)
        await card.getByLabel('Адрес возврата').fill(base.redirectUrl)
        await card.getByRole('button', { name: 'Сохранить' }).click()
        await expect(page.getByText('Список «кого пускать» пуст — через Authentik не войдёт никто').first()).toBeVisible()

        // Пароль без рабочего SSO не выключается.
        await card.getByLabel('Вход через Authentik').click()
        await card.getByLabel('Оставить вход по паролю').click()
        await card.getByRole('button', { name: 'Сохранить' }).click()
        await expect(page.getByText('Без SSO парольный вход выключить нельзя').first()).toBeVisible()

        // И с SSO — не из сессии, вошедшей паролем.
        const viaPassword = await page.request.put('/api/admin/sso', { data: { ...base, passwordLogin: false } })
        expect(viaPassword.status()).toBe(400)
        expect((await viaPassword.json()).statusMessage).toBe('sso_not_proven')
        const http = await page.request.put('/api/admin/sso', { data: { ...base, redirectUrl: 'http://vpn2.example.invalid/auth/authentik' } })
        expect((await http.json()).statusMessage).toBe('sso_bad_redirect')
        const noUser = await page.request.put('/api/admin/sso', { data: { ...base, allowedSubs: `${base.allowedSubs}:nobody-e2e` } })
        expect((await noUser.json()).statusMessage).toBe('sso_no_local_user')

        // «Проверить» читает discovery: недоступный адрес — понятная ошибка.
        const check = await page.request.post('/api/admin/sso/check', { data: { authentikUrl: 'http://127.0.0.1:9' } })
        expect(check.status()).toBe(400)
        expect((await check.json()).message).toMatch(/^Discovery не прочитан/)

        // Рабочая настройка сохраняется и сразу видна странице входа.
        expect((await page.request.put('/api/admin/sso', { data: base })).status()).toBe(200)
        const saved = await page.request.get('/api/admin/sso')
        const text = await saved.text()
        expect(text).not.toContain(base.clientSecret)
        const st = JSON.parse(text)
        expect(st).toMatchObject({ enabled: true, hasSecret: true, source: 'panel', clientId: base.clientId })
        expect(st.discoveryUrl).toBe(`${base.authentikUrl}/application/o/${base.appSlug}/.well-known/openid-configuration`)
        // Пустой секрет при сохранении — прежний остаётся.
        expect((await page.request.put('/api/admin/sso', { data: { ...base, clientSecret: '' } })).status()).toBe(200)
        expect((await (await page.request.get('/api/admin/sso')).json()).hasSecret).toBe(true)

        const anon = await browser.newContext({ baseURL: new URL(page.url()).origin })
        const p = await anon.newPage()
        expect(await (await p.request.get('/api/sso')).json()).toEqual({ enabled: true, autoRedirect: false, label: base.label })
        await p.goto('/login')
        await expect(p.getByRole('link', { name: `Войти через ${base.label}` })).toBeVisible()
        await anon.close()
      }
      finally {
        await page.request.put('/api/admin/sso', { data: { ...base, enabled: false } })
      }
      expect(await (await page.request.get('/api/sso')).json()).toMatchObject({ enabled: false })
    })
  })
})

test.describe('me-redirect', () => {
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
})

test.describe('mobile-login', { tag: '@mobile' }, () => {
  /** Вход через Authentik включён: на экране входа есть кнопка SSO. */
  const sso = {
    enabled: true,
    authentikUrl: 'https://auth.example.invalid',
    appSlug: 'vpn2-e2e',
    clientId: 'vpn2-e2e',
    clientSecret: 'e2e-client-secret-0123456789',
    allowedSubs: '00000000-0000-4000-8000-000000000001',
    label: 'Authentik',
    autoRedirect: false,
    passwordLogin: true,
    redirectUrl: 'https://vpn2.example.invalid/auth/authentik',
  }

  async function px(l: Locator, prop: string) {
    return l.evaluate((el, p) => Number.parseFloat(getComputedStyle(el).getPropertyValue(p)), prop)
  }

  /** Карточка во всю ширину с полями 16px и по центру экрана по высоте. */
  async function cardFits(page: Page, where: string) {
    const vp = page.viewportSize()!
    const card = page.locator('.lgn')
    // Карточка выплывает и встряхивается при ошибке: мерить после движения.
    await card.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished)))
    const box = (await card.boundingBox())!
    expect(Math.round(box.x), `${where}: отступ карточки слева`).toBe(16)
    expect(Math.round(box.width), `${where}: ширина карточки`).toBe(vp.width - 32)
    const below = vp.height - (box.y + box.height)
    expect(Math.abs(box.y - below), `${where}: карточка по центру (сверху ${box.y}, снизу ${below})`).toBeLessThanOrEqual(2)
  }

  /** Поля и кнопки под палец; шрифт полей не меньше 16px — Safari не увеличит страницу при фокусе. */
  async function touchSized(page: Page, where: string) {
    for (const l of await page.locator('.lgn .in').all()) {
      expect(await px(l, 'height'), `${where}: высота поля`).toBe(46)
      expect(await px(l, 'font-size'), `${where}: шрифт поля`).toBeGreaterThanOrEqual(16)
    }
    for (const l of await page.locator('.lgn .pill').all()) {
      expect(await px(l, 'height'), `${where}: высота кнопки`).toBe(46)
    }
  }

  for (const width of [360, 402]) {
    test.describe(`телефон ${width}px`, () => {
      test.use({ viewport: { width, height: 874 } })

      test('вход: обе темы по макету без горизонтальной прокрутки, пароль и код из аутентификатора, неверный код виден', async ({ page, guard, request }) => {
        test.setTimeout(180_000)
        guard.allow(401, /\/api\/auth\/two-factor\/verify-totp$/, 'POST')
        // Настройки — отдельной сессией API, страница остаётся без входа.
        const signIn = await authPost(request, '/sign-in/username', { username: ADMIN_USER, password: ADMIN_PASSWORD })
        expect(signIn.status(), await signIn.text()).toBe(200)
        expect((await request.put('/api/admin/sso', { data: sso })).status()).toBe(200)
        let secret = ''
        let usedStep = 0
        let enabled = false
        // Панель гасит принятый шаг: каждому подтверждению — шаг, которого она ещё не видела.
        const fresh = async () => {
          const now = Math.floor(Date.now() / 30_000)
          const step = Math.max(now, usedStep + 1)
          if (step > now + 1) await page.waitForTimeout((step - 1) * 30_000 - Date.now() + 500)
          usedStep = step
          return totp(secret, step * 30_000)
        }
        try {
          for (const scheme of ['dark', 'light'] as const) {
            await page.emulateMedia({ colorScheme: scheme })
            await page.goto('/login')
            await hydrated(page)
            await expect(page.getByRole('link', { name: 'Войти через Authentik' })).toBeVisible()
            await expect(page.getByText('или по паролю')).toBeVisible()
            await cardFits(page, `${scheme}, шаг пароля`)
            await touchSized(page, `${scheme}, шаг пароля`)
            await noHorizontalOverflow(page, `/login, ${scheme}`)
          }

          const en = await authPost(request, '/two-factor/enable', { password: ADMIN_PASSWORD })
          expect(en.status(), await en.text()).toBe(200)
          secret = new URL((await en.json()).totpURI).searchParams.get('secret') ?? ''
          const on = await authPost(request, '/two-factor/verify-totp', { code: await fresh() })
          expect(on.status(), await on.text()).toBe(200)
          enabled = true

          await page.getByLabel('Логин').fill(ADMIN_USER)
          await page.getByLabel('Пароль').fill(ADMIN_PASSWORD)
          await page.getByRole('button', { name: 'Войти' }).tap()
          const code = page.getByLabel('Код из аутентификатора')
          await expect(code).toBeVisible()
          expect(await px(code, 'font-size'), 'шрифт кода').toBe(17)
          expect(await px(code, 'letter-spacing'), 'разрядка кода').toBeCloseTo(17 * 0.12, 1)
          await expect(page.getByRole('button', { name: 'Назад' })).toBeVisible()
          await touchSized(page, 'шаг кода')

          const wrong = String((Number(totp(secret)) + 500_000) % 1_000_000).padStart(6, '0')
          await code.fill(wrong)
          await page.getByRole('button', { name: 'Подтвердить' }).tap()
          const err = page.locator('form .al.red')
          await expect(err).toHaveText('Код не подошёл')
          await expect(err).toBeInViewport({ ratio: 1 })
          await cardFits(page, 'неверный код')
          await noHorizontalOverflow(page, '/login, неверный код')
          await code.tap()
          expect(await page.evaluate(() => window.visualViewport?.scale ?? 1), 'масштаб после фокуса в поле').toBe(1)

          await code.fill(await fresh())
          await page.getByRole('button', { name: 'Подтвердить' }).tap()
          await expect(page).toHaveURL(/\/$/)
          await expect(page.locator('.mbar nav')).toBeVisible()
        }
        finally {
          // Упавший тест не оставляет admin с 2FA и включённым SSO.
          // Выключение 2FA гасит сессию страницы, открытый Обзор поймал бы 401.
          await page.goto('about:blank')
          if (enabled) {
            const off = await authPost(request, '/two-factor/disable', { password: ADMIN_PASSWORD, code: await fresh() })
            expect(off.status(), await off.text()).toBe(200)
          }
          expect((await request.put('/api/admin/sso', { data: { ...sso, enabled: false } })).status()).toBe(200)
          expect(await (await request.get('/api/sso')).json()).toMatchObject({ enabled: false })
        }
      })
    })
  }
})

test.describe('mobile-settings', { tag: '@mobile' }, () => {
  // Настройки на телефоне: одна колонка, подпись над полем, «Выключить 2FA?» — окном.

  async function openSettings(page: Page) {
    await page.goto('/settings')
    await hydrated(page)
    await expect(page.locator('.stg fieldset.fg2')).toBeEnabled()
  }

  async function logoutUi(page: Page) {
    await page.getByRole('button', { name: /^Меню @/ }).tap()
    await page.getByRole('menuitem', { name: 'Выйти' }).tap()
    await expect(page).toHaveURL(/\/login/)
    await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
  }

  test.describe('настройки на телефоне', () => {
    for (const width of [360, 402]) {
      for (const scheme of ['dark', 'light'] as const) {
        test(`${width} ${scheme}: одна колонка, поля целиком, вбок не едет`, async ({ page }) => {
          await page.setViewportSize({ width, height: 874 })
          await page.emulateMedia({ colorScheme: scheme })
          await loginApi(page)
          await openSettings(page)

          const cards = page.locator('.stg .sgrid > .card')
          await expect(cards).toHaveCount(3)
          const boxes = await cards.evaluateAll(els => els.map((el) => {
            const r = el.getBoundingClientRect()
            return { left: Math.round(r.left), width: Math.round(r.width) }
          }))
          const inner = await page.locator('.stg .sgrid').evaluate(el => el.clientWidth)
          for (const b of boxes) {
            expect(b.left, 'карточки стоят одна под другой').toBe(boxes[0]!.left)
            expect(b.width, 'карточка во всю ширину').toBe(inner)
          }

          // Каждое поле ввода: подпись над ним, ширина поля почти во всю карточку.
          const fields = await page.locator('.stg .sc label.fld').evaluateAll(els => els.map((el) => {
            const span = el.querySelector(':scope > span')!.getBoundingClientRect()
            const input = el.querySelector('input')!.getBoundingClientRect()
            const card = el.closest('.sc')!.getBoundingClientRect()
            return { name: el.querySelector(':scope > span')!.textContent, labelBottom: span.bottom, inputTop: input.top, input: input.width, card: card.width }
          }))
          expect(fields.length).toBeGreaterThanOrEqual(10)
          for (const f of fields) {
            expect(f.labelBottom, `«${f.name}»: подпись над полем`).toBeLessThanOrEqual(f.inputTop)
            expect(f.input, `«${f.name}»: поле не обрезано`).toBeGreaterThan(f.card * 0.6)
          }
          await noHorizontalOverflow(page, `/settings ${width} ${scheme}`)
          await page.getByRole('switch', { name: 'Вход через Authentik' }).tap()
          await noHorizontalOverflow(page, `/settings ${width} ${scheme}, Authentik вкл.`)
        })
      }
    }

    test('2FA: включить, войти с кодом, выключить в окне, войти без кода', async ({ page }) => {
      test.setTimeout(150_000)
      await loginApi(page)
      await openSettings(page)
      await page.locator('#tfa').getByLabel('Текущий пароль').fill(ADMIN_PASSWORD)
      await page.getByRole('button', { name: 'Включить 2FA' }).tap()
      const secretInput = page.locator('#tfa input[readonly]')
      await expect(secretInput).toHaveValue(/^[A-Z2-7]{16,}$/)
      const secret = await secretInput.inputValue()
      // Принятый шаг гасится: каждому коду — шаг, которого панель ещё не видела.
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
        await page.getByLabel('Введи 6-значный код из приложения').fill(await fresh())
        await page.getByRole('button', { name: 'Подтвердить' }).tap()
        enabled = true
        await expect(toast(page, '2FA включена')).toBeVisible()

        await logoutUi(page)
        await page.getByLabel('Логин').fill(ADMIN_USER)
        await page.getByLabel('Пароль').fill(ADMIN_PASSWORD)
        await page.getByRole('button', { name: 'Войти' }).tap()
        const code = page.getByPlaceholder('123 456')
        await expect(code).toBeVisible()
        await code.fill(await fresh())
        await page.getByRole('button', { name: 'Подтвердить' }).tap()
        await expect(page).toHaveURL(/\/$/)

        await openSettings(page)
        await expect(page.getByText('Включена', { exact: true })).toBeVisible()
        await page.getByRole('button', { name: 'Выключить 2FA' }).tap()
        const sheet = page.getByRole('dialog', { name: 'Выключить 2FA?' })
        await expect(sheet).toBeVisible()
        await noHorizontalOverflow(page, 'окно «Выключить 2FA?»')
        // «Отмена» ничего не меняет.
        await sheet.getByRole('button', { name: 'Отмена' }).tap()
        await expect(sheet).toBeHidden()
        await expect(page.getByText('Включена', { exact: true })).toBeVisible()

        await page.getByRole('button', { name: 'Выключить 2FA' }).tap()
        await sheet.getByLabel('Пароль', { exact: true }).fill(ADMIN_PASSWORD)
        await sheet.getByLabel('Код 2FA (6 цифр)').fill(await fresh())
        await sheet.getByRole('button', { name: 'Выключить' }).tap()
        await expect(toast(page, '2FA выключена')).toBeVisible()
        enabled = false
        await expect(sheet).toBeHidden()
        await expect(page.getByText('Выключена', { exact: true })).toBeVisible()
      }
      finally {
        if (enabled) {
          await page.context().clearCookies()
          await authPost(page.request, '/sign-in/username', { username: ADMIN_USER, password: ADMIN_PASSWORD }).catch(() => {})
          await authPost(page.request, '/two-factor/verify-totp', { code: await fresh() }).catch(() => {})
          await authPost(page.request, '/two-factor/disable', { password: ADMIN_PASSWORD, code: await fresh() }).catch(() => {})
        }
      }
      // Вход снова без кода.
      await page.context().clearCookies()
      await loginUi(page)
      await expect(page).toHaveURL(/\/$/)
    })

    test('смена пароля, затем вход с новым', async ({ page }) => {
      await loginApi(page)
      await openSettings(page)
      const form = page.locator('form').filter({ has: page.getByRole('button', { name: 'Сохранить пароль' }) })
      const temp = `${ADMIN_PASSWORD}-m388`
      let changed = false
      try {
        await form.getByLabel('Текущий пароль').fill(ADMIN_PASSWORD)
        await form.getByLabel('Новый пароль').fill(temp)
        await form.getByLabel('Повторите').fill(temp)
        await form.getByRole('button', { name: 'Сохранить пароль' }).tap()
        await expect(toast(page, 'Пароль обновлён')).toBeVisible()
        changed = true
        await page.context().clearCookies()
        await loginUi(page, temp)
        await expect(page).toHaveURL(/\/$/)
      }
      finally {
        if (changed) {
          // Открытая главная опрашивает API: после снятия cookie это 401 в журнал.
          await page.goto('about:blank')
          await page.context().clearCookies()
          await authPost(page.request, '/sign-in/username', { username: ADMIN_USER, password: temp })
          const back = await authPost(page.request, '/change-password', { currentPassword: temp, newPassword: ADMIN_PASSWORD })
          expect(back.status()).toBe(200)
        }
      }
      await page.context().clearCookies()
      await loginApi(page)
    })

    test('бот и Authentik сохраняются с телефона', async ({ page }) => {
      await loginApi(page)
      const tg = await (await page.request.get('/api/admin/telegram')).json()
      const sso = await (await page.request.get('/api/admin/sso')).json()
      try {
        await openSettings(page)
        const bot = page.locator('.sbot')
        const nick = bot.getByLabel('Никнейм админа бота', { exact: true })
        const temp = `m388_${Date.now().toString(36)}`
        await nick.fill(temp)
        await bot.getByRole('button', { name: 'Сохранить' }).tap()
        await expect(toast(page, 'Настройки бота сохранены')).toBeVisible()
        expect((await (await page.request.get('/api/admin/telegram')).json()).admin_username).toBe(temp)

        const card = page.locator('form').filter({ hasText: 'Кого пускать' })
        await card.getByLabel('Подпись кнопки').fill('Вход m388')
        await card.getByRole('button', { name: 'Сохранить' }).tap()
        await expect(toast(page, 'Настройки входа сохранены')).toBeVisible()
        expect((await (await page.request.get('/api/admin/sso')).json()).label).toBe('Вход m388')
      }
      finally {
        await page.request.put('/api/admin/telegram', { data: { admin_username: tg.admin_username } })
        await page.request.put('/api/admin/sso', { data: { ...sso, clientSecret: '' } })
      }
      expect((await (await page.request.get('/api/admin/sso')).json()).label).toBe(sso.label)
    })
  })
})
