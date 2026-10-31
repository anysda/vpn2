import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { ADMIN_PASSWORD, ADMIN_USER, expect, hydrated, test } from './support/fixtures'
import { container } from './support/openvpn-ca'

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
