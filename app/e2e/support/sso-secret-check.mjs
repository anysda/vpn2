import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { setTimeout as delay } from 'node:timers/promises'

// Отдельный одноразовый контейнер: проверяем реальный Nitro/Better Auth,
// включая старый env и runtime из архива, не подменяя API ответами теста.
const runtime = '/etc/anysda/sso-runtime.json'
const origin = 'http://127.0.0.1:51821'
const password = randomBytes(18).toString('hex')
const profile = {
  enabled: true, authentikUrl: 'https://auth.example.test', appSlug: 'vpn2',
  clientId: 'vpn2', clientSecret: '', allowedSubs: 'test-sub', label: 'Authentik',
  autoRedirect: true, passwordLogin: false, redirectUrl: 'https://vpn.example.test/auth/authentik',
}
mkdirSync('/etc/anysda', { recursive: true })

for (const scenario of ['seed', 'restore-empty', 'restore-whitespace', 'restore-valid', 'reset-runtime']) {
  if (scenario.startsWith('restore-')) {
    writeFileSync(runtime, JSON.stringify({
      ...profile,
      clientSecret: scenario === 'restore-valid' ? 'test-only-secret' : scenario === 'restore-whitespace' ? '  ' : '',
    }), { mode: 0o600 })
  }
  else {
    rmSync(runtime, { force: true })
  }
  const child = spawn(process.execPath, ['.output/server/index.mjs'], {
    env: {
      ...process.env, HOST: '127.0.0.1', PORT: '51821',
      NUXT_DATABASE_URL: 'file:/var/lib/anysda-vpn2/db.sqlite',
      NUXT_ANYSDA_CONFIG_PATH: '/nonexistent/config.yaml',
      NUXT_ADMIN_USER: 'admin', NUXT_ADMIN_PASSWORD: password,
      NUXT_SESSION_PASSWORD: randomBytes(32).toString('hex'), NUXT_SESSION_COOKIE_SECURE: 'false',
      NUXT_PUBLIC_SSO_ENABLED: 'true', NUXT_PUBLIC_SSO_AUTO_REDIRECT: 'true',
      NUXT_SSO_PASSWORD_LOGIN: 'false', NUXT_OAUTH_OIDC_CLIENT_ID: 'vpn2',
      NUXT_OAUTH_OIDC_OPENID_CONFIG: 'https://auth.example.test/application/o/vpn2/.well-known/openid-configuration',
      NUXT_OAUTH_OIDC_CLIENT_SECRET: '',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let output = ''
  child.stdout.on('data', b => { output += b })
  child.stderr.on('data', b => { output += b })
  const exited = new Promise((resolve, reject) => {
    child.on('exit', resolve)
    child.on('error', reject)
  })
  try {
    let ready = false
    for (let attempt = 0; attempt < 100; attempt++) {
      ready = await fetch(`${origin}/api/version`).then(r => r.ok).catch(() => false)
      if ((ready && (scenario !== 'seed' || output.includes('admin user created'))) || child.exitCode !== null) break
      await delay(100)
    }
    assert.ok(ready, output)
    if (scenario === 'seed') assert.ok(output.includes('admin user created'), output)
    const state = await (await fetch(`${origin}/api/sso`)).json()
    console.log(`${scenario}: GET /api/sso → ${JSON.stringify(state)}`)
    const valid = scenario === 'restore-valid'
    assert.deepEqual(state, { enabled: valid, autoRedirect: valid, label: 'Authentik' })
    const login = await fetch(`${origin}/api/auth/sign-in/username`, {
      method: 'POST', headers: { origin, 'content-type': 'application/json' },
      body: JSON.stringify({ username: 'admin', password }),
    })
    console.log(`${scenario}: password login → ${login.status}`)
    assert.equal(login.status, valid ? 403 : 200, await login.text())
    if (valid) continue
    const route = await fetch(`${origin}/auth/authentik`, { redirect: 'manual' })
    assert.equal(route.headers.get('location'), '/login?direct=1&error=sso_off')
    const headers = {
      origin, 'content-type': 'application/json',
      cookie: login.headers.getSetCookie().map(c => c.split(';')[0]).join('; '),
    }
    const save = data => fetch(`${origin}/api/admin/sso`, {
      method: 'PUT', headers, body: JSON.stringify(data),
    })
    const rejected = await save({ ...profile, passwordLogin: true })
    assert.equal(rejected.status, 400)
    assert.equal((await rejected.json()).statusMessage, 'sso_no_client_secret')
    console.log(`${scenario}: save enabled with empty secret → 400 sso_no_client_secret`)
    assert.equal((await save({ ...profile, enabled: false, passwordLogin: true })).status, 200)
    assert.equal((await save({ ...profile, clientSecret: 'test-only-secret', passwordLogin: true })).status, 200)
    // Пустое поле в профиле означает «сохранить прежний секрет».
    assert.equal((await save({ ...profile, passwordLogin: true })).status, 200)
    assert.equal((await (await fetch(`${origin}/api/sso`)).json()).enabled, true)
    console.log(`${scenario}: saved secret survives empty profile field → enabled:true`)
  }
  finally {
    child.kill('SIGTERM')
    const timer = setTimeout(() => child.kill('SIGKILL'), 5000)
    await exited.finally(() => clearTimeout(timer))
  }
}
