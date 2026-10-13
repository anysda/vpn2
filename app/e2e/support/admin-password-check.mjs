import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'

// Запускать в одноразовом образе панели с read-only /etc/anysda и пустой БД.
// Не печатаем журнал дочернего процесса: на старой версии в нём пароль.
const child = spawn(process.execPath, ['.output/server/index.mjs'], {
  env: {
    ...process.env,
    PORT: '31821',
    NUXT_DATABASE_URL: 'file:/var/lib/anysda-vpn2/db.sqlite',
    NUXT_ANYSDA_CONFIG_PATH: '/nonexistent/config.yaml',
    NUXT_ADMIN_PASSWORD: '',
    NUXT_SESSION_PASSWORD: randomBytes(32).toString('hex'),
  },
  stdio: ['ignore', 'pipe', 'pipe'],
})
let output = ''
child.stdout.on('data', b => { output += b })
child.stderr.on('data', b => { output += b })
const timer = setTimeout(() => child.kill('SIGKILL'), 8000)
const code = await new Promise((resolve, reject) => {
  child.on('error', reject)
  child.on('exit', resolve)
}).finally(() => clearTimeout(timer))
assert.equal(output.includes('migrations applied'), true, 'должны пройти миграции пустой БД')
assert.equal(/GENERATED ADMIN PASSWORD|"password"\s*:/.test(output), false, 'пароль администратора утёк в журнал')
assert.equal(code, 1, 'при невозможности сохранить пароль панель должна завершиться с кодом 1')
console.log('admin password: read-only storage fails closed without leaking credentials')
