import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { createSocket } from 'node:dgram'
import { once } from 'node:events'
import { mkdirSync, writeFileSync } from 'node:fs'
import { setTimeout as delay } from 'node:timers/promises'
import { createServer } from 'node:net'

// Explicit deploy step before starting the application under test.
assert.equal(spawnSync(process.execPath, ['scripts/migrate.mjs'], {
  env: { ...process.env, NUXT_DATABASE_URL: 'file:/var/lib/anysda-vpn2/db.sqlite' },
  stdio: 'pipe',
}).status, 0, 'deploy migration must succeed')

// Реальный Nitro в одноразовом контейнере: ошибки должны дойти до панели
// в JSON, а не превратиться в скрытую production-ошибку 500.
mkdirSync('/etc/wireguard', { recursive: true })
for (const name of ['server.priv', 'server.pub']) {
  writeFileSync(`/etc/wireguard/${name}`, 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=', { mode: 0o600 })
}
const password = randomBytes(16).toString('hex')
// This fixture tests DNS and profile delivery, with a typed control boundary.
const controlSocket = '/tmp/wg-endpoint-control.sock'
let peers = {}
const controlServer = createServer(socket => {
  let input = ''
  socket.on('data', chunk => {
    input += chunk
    if (!input.includes('\n')) return
    const { method, params } = JSON.parse(input)
    let result = {}
    if (method === 'profile.read') result = { publicKey: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=' }
    if (method === 'state.read') result = { peers }
    if (method === 'wg.apply') peers = Object.fromEntries(params.peers.map(peer => [peer.key, [peer.ip + '/32']]))
    socket.end(JSON.stringify({ api: 1, ok: true, result }) + '\n')
  })
})
await new Promise(resolve => controlServer.listen(controlSocket, resolve))
const base = 'http://127.0.0.1:31821'
// Локальный DNS: настоящие ответы AAAA / только A и сервер без ответа.
// Внешняя сеть контейнеру не нужна, production-код не содержит тестовых хуков.
const dns = createSocket('udp4')
await new Promise(resolve => dns.bind(0, '127.0.0.1', resolve))
let dnsMode = ''
let dnsQueries = 0
dns.on('message', (query, remote) => {
  dnsQueries++
  if (dnsMode === 'unavailable') return
  const reply = Buffer.from(query)
  reply.writeUInt16BE(0x8180, 2)
  const hasAAAA = dnsMode === 'aaaa'
  reply.writeUInt16BE(hasAAAA ? 1 : 0, 6)
  const answer = Buffer.from('c00c001c00010000003c001020010db8000000000000000000000001', 'hex')
  dns.send(hasAAAA ? Buffer.concat([reply, answer]) : reply, remote.port, remote.address)
})
// Каждый Resolver имеет свой список серверов: глобальный setServers()
// не меняет новые экземпляры. Меняем только адрес DNS, сами запросы настоящие.
const dnsSetup = `import { Resolver } from 'node:dns/promises';
const resolve6 = Resolver.prototype.resolve6;
Resolver.prototype.resolve6 = function (...args) {
  this.setServers(['127.0.0.1:${dns.address().port}']);
  return resolve6.apply(this, args);
}`
const scenarios = [
  ...['aaaa', 'a-only', 'unavailable'].map(mode => ({ entryIp: '203.0.113.7', host: 'vpn.example.test', mode })),
  { entryIp: '203.0.113.7', host: '[2001:db8::1]' },
  { entryIp: '2001:db8::1', host: '[2001:db8::1]' },
]
try {
  for (const { entryIp, host, mode } of scenarios) {
    dnsMode = mode || ''
    dnsQueries = 0
    const child = spawn(process.execPath, ['--import', `data:text/javascript,${encodeURIComponent(dnsSetup)}`, '.output/server/index.mjs'], {
      env: {
        ...process.env,
        VPN2_CONTROL_SOCKET: controlSocket,
        HOST: '127.0.0.1',
        PORT: '31821',
        NUXT_DATABASE_URL: 'file:/var/lib/anysda-vpn2/db.sqlite',
        NUXT_ADMIN_PASSWORD: password,
        NUXT_SESSION_PASSWORD: password.repeat(2),
        NUXT_SESSION_COOKIE_SECURE: 'false',
        NUXT_WG_ENABLED: 'true',
        NUXT_WG_PUBLIC_HOST: host,
        NUXT_WG_ENDPOINT_IP: entryIp,
        NUXT_TGBOT_SECRET: password,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let logs = ''
    child.stdout.on('data', chunk => { logs += chunk })
    child.stderr.on('data', chunk => { logs += chunk })
    const stopped = once(child, 'exit')
    let cookie = ''
    async function api(path, data) {
      return fetch(base + path, {
        method: data === undefined ? 'GET' : 'POST',
        headers: { origin: base, cookie, 'content-type': 'application/json', authorization: `Bearer ${password}` },
        body: data === undefined ? undefined : JSON.stringify(data),
        signal: AbortSignal.timeout(10000),
      })
    }
    try {
      let ready = false
      for (let attempt = 0; attempt < 100; attempt++) {
        ready = await fetch(base + '/api/version').then(r => r.ok).catch(() => false)
        if (ready) break
        await delay(100)
      }
      assert(ready, 'панель должна запуститься')
      // /api/version отвечает до async init: ждём также миграций и синхронизации
      // существующего администратора, независимо от порядка DNS-сценариев.
      for (let attempt = 0; attempt < 100 && !logs.includes('panel init complete'); attempt++) {
        await delay(100)
      }
      assert(logs.includes('panel init complete'), `инициализация должна завершиться: ${logs}`)
      if (mode) assert.equal((await api('/api/wg-endpoint-warning')).status, 401)
      const login = await api('/api/auth/sign-in/username', { username: 'admin', password })
      assert.equal(login.status, 200)
      cookie = login.headers.getSetCookie().map(c => c.split(';')[0]).join('; ')
      const created = await api('/api/clients', { name: `e2e-wg-${mode || entryIp}` })
      assert.equal(created.status, 200)
      const client = await created.json()
      const added = await api(`/api/clients/${client.id}/devices`, { name: 'phone' })
      assert.equal(added.status, 200)
      const device = await added.json()
      const { DatabaseSync } = await import('node:sqlite')
      const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite')
      db.exec('PRAGMA busy_timeout = 10000')
      db.prepare('UPDATE clients SET tg_chat_id = ? WHERE id = ?').run(client.id, client.id)
      db.close()
      const prefix = `/api/clients/${client.id}/devices/${device.id}`
      if (mode) {
        const start = performance.now()
        const warnings = await Promise.all(Array.from({ length: 4 }, () => api('/api/wg-endpoint-warning')))
        for (const warning of warnings) {
          assert.equal(warning.status, 200)
          assert.deepEqual(await warning.json(), { hasAAAA: mode === 'aaaa' })
        }
        assert(performance.now() - start < 1000, 'DNS-проверка должна завершаться за долю секунды')
        assert.equal(dnsQueries, 1, 'параллельные запросы используют одну DNS-проверку')
        assert.deepEqual(await (await api('/api/wg-endpoint-warning')).json(), { hasAAAA: mode === 'aaaa' })
        assert.equal(dnsQueries, 1, 'результат, включая ошибку DNS, должен кэшироваться')
      }
      for (const suffix of ['wg-config', 'wg-qrcode.svg', 'send-wg-to-tg', 'bot']) {
        const path = suffix === 'bot' ? `/api/bot/devices/${device.id}/wg-config?chatId=${client.id}` : `${prefix}/${suffix}`
        const response = await api(path, suffix === 'send-wg-to-tg' ? {} : undefined)
        if (entryIp.includes(':')) {
          assert.equal(response.status, 422, suffix)
          const error = await response.json()
          assert.equal(error.statusMessage, 'wg_ipv4_endpoint_unavailable')
          assert.match(error.message, /IPv4.*сервера/)
          assert.match(error.message, /entry.public_host/)
        }
        else if (suffix === 'send-wg-to-tg') {
          // Бота нет: достижение отправки означает успешную генерацию профиля.
          assert.equal(response.status, 502)
          assert.equal((await response.json()).statusMessage, 'bot_unreachable')
        }
        else {
          assert.equal(response.status, 200, suffix)
          const body = await response.text()
          if (suffix === 'wg-qrcode.svg') assert.match(body, /^<svg/)
          else {
            assert(body.includes(`Endpoint = ${mode ? host : entryIp}:51820\n`))
            assert(Buffer.byteLength(body) <= 2953)
          }
        }
      }
      assert.doesNotMatch(logs, /ETIMEOUT|ENODATA|ENOTFOUND|ECONNREFUSED|ECANCELLED/)
      console.log(mode
        ? `WG DNS ${mode}: warning=${mode === 'aaaa'}, domain preserved, all four APIs pass, DNS cached and silent`
        : `WG delivery ${entryIp.includes(':') ? 'without IPv4: all four APIs return actionable 422' : 'with IPv4: config, QR, bot config and send generation pass'}`)
    }
    finally {
      child.kill('SIGTERM')
      const timer = setTimeout(() => child.kill('SIGKILL'), 5000)
      await stopped.finally(() => clearTimeout(timer))
    }
  }
}
finally {
  dns.close()
  controlServer.close()
}
