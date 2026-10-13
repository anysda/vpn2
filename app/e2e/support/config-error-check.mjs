import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { writeFileSync } from 'node:fs'

// Парсер YAML включает исходную строку в исключение. Секрет не должен
// попасть ни в logger, ни в console.error обработчика ошибки инициализации.
const secret = randomBytes(24).toString('hex')
const config = '/tmp/invalid-panel-config.yaml'
writeFileSync(config, `admin:\n  user: admin\n  password: !invalid ${secret}\n`, { mode: 0o600 })
const child = spawn(process.execPath, ['.output/server/index.mjs'], {
  env: {
    ...process.env,
    PORT: '31821',
    NUXT_DATABASE_URL: 'file:/var/lib/anysda-vpn2/db.sqlite',
    NUXT_ANYSDA_CONFIG_PATH: config,
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
assert.equal(output.includes(secret), false, 'пароль из некорректного YAML утёк в журнал')
assert.equal(code, 1, 'некорректный конфиг должен останавливать запуск')
assert.equal(output.includes(config), true, 'в ошибке должен остаться путь к некорректному конфигу')
console.log('config error: invalid YAML fails closed without leaking source text')
