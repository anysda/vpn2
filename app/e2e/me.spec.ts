import { ADMIN_PASSWORD, ADMIN_USER, authPost, expect, hydrated, loginApi, loginUi, test, toast } from './support/fixtures'

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
      const noSecret = await page.request.put('/api/admin/sso', { data: { ...base, clientSecret: '' } })
      expect((await noSecret.json()).statusMessage).toBe('sso_no_client_secret')
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
