import { expect, hydrated, loginApi, ADMIN_PASSWORD, ADMIN_USER, test } from './support/fixtures'
import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { container } from './support/openvpn-ca'

test.describe('sso-load-error', () => {
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
})

test.describe('sso-missing-secret', () => {
  test('засев без секрета оставляет форму и парольный вход', async ({ page, guard }) => {
    test.skip(!container, 'нужен локальный образ из run-local.sh')
    guard.allow(400, /\/api\/admin\/sso$/)
    const name = `${container}-sso-browser`
    const stand = path.resolve('e2e/support/panel-stand.sh')
    const shell = (code: string) => execFileSync('bash', ['-ec', 'source "$1"; ' + code,
      'sso-browser', stand, name, container!, ADMIN_PASSWORD], { encoding: 'utf8' }).trim()
    try {
      const address = shell(`
      image=$(dk inspect --format '{{.Config.Image}}' "$3")
      stand_up "$2" "$image" 127.0.0.1:0 "$4" --tmpfs /var/lib/anysda-vpn2 \\
        -e NUXT_PUBLIC_SSO_ENABLED=true -e NUXT_PUBLIC_SSO_AUTO_REDIRECT=true \\
        -e NUXT_SSO_PASSWORD_LOGIN=false -e NUXT_OAUTH_OIDC_CLIENT_SECRET= \\
        -e NUXT_OAUTH_OIDC_CLIENT_ID=vpn2 \\
        -e NUXT_OAUTH_OIDC_OPENID_CONFIG=https://auth.example.test/application/o/vpn2/.well-known/openid-configuration
      dk port "$2-caddy" 80
    `)
      const origin = `http://${address}`
      await page.context().setExtraHTTPHeaders({ origin })
      await expect.poll(async () => page.request.get(`${origin}/api/version`).then(r => r.status()).catch(() => 0)).toBe(200)
      const state = await (await page.request.get(`${origin}/api/sso`)).json()
      console.log(`isolated seed: GET /api/sso → ${JSON.stringify(state)}`)
      expect(state).toMatchObject({ enabled: false, autoRedirect: false })
      const external: string[] = []
      page.on('request', (request) => {
        if (new URL(request.url()).hostname === 'auth.example.test') external.push(request.url())
      })
      await page.goto(origin)
      await hydrated(page)
      await expect(page).toHaveURL(`${origin}/login`)
      await page.getByLabel('Логин').fill(ADMIN_USER)
      await page.getByLabel('Пароль', { exact: true }).fill(ADMIN_PASSWORD)
      await page.getByRole('button', { name: 'Войти' }).click()
      await expect(page).toHaveURL(`${origin}/`)
      await expect(page.getByRole('button', { name: `@${ADMIN_USER}` })).toBeVisible()
      expect(external).toEqual([])
      console.log('isolated seed: / → /login; password form → /; Authentik requests=0')

      // Отказ без секрета проверяем здесь: в общем стенде секрет уже сохранён
      // прошлым прогоном, а пустой clientSecret при сохранении оставляет прежний.
      const noSecret = await page.request.put(`${origin}/api/admin/sso`, { data: {
        enabled: true,
        authentikUrl: 'https://auth.example.invalid',
        appSlug: 'vpn2-e2e',
        clientId: 'vpn2-e2e',
        clientSecret: '',
        allowedSubs: '00000000-0000-4000-8000-000000000001',
        label: 'E2E IdP',
        autoRedirect: false,
        passwordLogin: true,
        redirectUrl: 'https://vpn2.example.invalid/auth/authentik',
      } })
      expect(noSecret.status()).toBe(400)
      expect((await noSecret.json()).statusMessage).toBe('sso_no_client_secret')
      await page.goto('about:blank')
    }
    finally {
      shell('stand_down "$2"')
    }
  })
})
