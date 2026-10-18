import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'

// Explicit deploy step before starting the application under test.
assert.equal(spawnSync(process.execPath, ['scripts/migrate.mjs'], {
  env: { ...process.env, NUXT_DATABASE_URL: 'file:/var/lib/anysda-vpn2/db.sqlite' },
  stdio: 'pipe',
}).status, 0, 'deploy migration must succeed')

// config.yaml и старый вход не ограничивали длину существующего пароля.
// Проверяем на одноразовой панели; новый пароль по-прежнему не длиннее 256.
const password = randomBytes(129).toString('hex')
const origin = 'http://127.0.0.1:31821'
const child = spawn(process.execPath, ['.output/server/index.mjs'], {
  env: {
    ...process.env,
    HOST: '127.0.0.1',
    PORT: '31821',
    NUXT_DATABASE_URL: 'file:/var/lib/anysda-vpn2/db.sqlite',
    NUXT_ANYSDA_CONFIG_PATH: '/nonexistent/config.yaml',
    NUXT_ADMIN_USER: 'admin',
    NUXT_ADMIN_PASSWORD: password,
    NUXT_SESSION_PASSWORD: randomBytes(32).toString('hex'),
    NUXT_SESSION_COOKIE_SECURE: 'false',
  },
  stdio: ['ignore', 'pipe', 'pipe'],
})
let output = ''
child.stdout.on('data', b => { output += b })
child.stderr.on('data', b => { output += b })
const exited = new Promise(resolve => child.on('exit', resolve))
const timer = setTimeout(() => child.kill('SIGKILL'), 15000)
async function post(path, body, cookie = '') {
  return fetch(`${origin}/api/auth${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', origin, cookie },
    body: JSON.stringify(body),
  })
}
try {
  for (let i = 0; i < 100 && !output.includes('admin user created'); i++) {
    assert.equal(child.exitCode, null, 'панель завершилась до создания администратора')
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  assert.ok(output.includes('admin user created'), 'панель должна применить пароль из конфига')
  const login = await post('/sign-in/username', { username: 'admin', password })
  assert.equal(login.status, 200, 'существующий пароль длиннее 256 должен позволять вход')
  const cookie = login.headers.getSetCookie().map(c => c.split(';')[0]).join('; ')
  const tooLong = await post('/change-password', { currentPassword: password, newPassword: `${password}x` }, cookie)
  assert.equal(tooLong.status, 400, 'новый пароль длиннее 256 должен отклоняться')
  assert.equal((await tooLong.json()).statusMessage, 'password_too_long')
  const changed = await post('/change-password', { currentPassword: password, newPassword: password.slice(0, 256) }, cookie)
  assert.equal(changed.status, 200, 'старый длинный пароль должен позволять смену на допустимый')
  const fresh = await post('/sign-in/username', { username: 'admin', password: password.slice(0, 256) })
  assert.equal(fresh.status, 200)
  console.log('long password: existing credential accepted, new password limited to 256')
}
finally {
  child.kill('SIGTERM')
  await exited
  clearTimeout(timer)
}
