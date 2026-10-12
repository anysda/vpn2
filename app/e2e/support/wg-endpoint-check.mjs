import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { once } from 'node:events'
import { mkdirSync, writeFileSync } from 'node:fs'
import { setTimeout as delay } from 'node:timers/promises'

// Реальный Nitro в одноразовом контейнере: ошибки должны дойти до панели
// в JSON, а не превратиться в скрытую production-ошибку 500.
mkdirSync('/etc/wireguard', { recursive: true })
for (const name of ['server.priv', 'server.pub']) {
  writeFileSync(`/etc/wireguard/${name}`, 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=', { mode: 0o600 })
}
const password = randomBytes(16).toString('hex')
const base = 'http://127.0.0.1:51821'
for (const entryIp of ['203.0.113.7', '2001:db8::1']) {
  const child = spawn(process.execPath, ['.output/server/index.mjs'], {
    env: {
      ...process.env,
      HOST: '127.0.0.1',
      NUXT_DATABASE_URL: 'file:/var/lib/anysda-vpn2/db.sqlite',
      NUXT_ADMIN_PASSWORD: password,
      NUXT_SESSION_PASSWORD: password.repeat(2),
      NUXT_SESSION_COOKIE_SECURE: 'false',
      NUXT_WG_ENABLED: 'true',
      NUXT_WG_PUBLIC_HOST: '[2001:db8::1]',
      NUXT_WG_ENDPOINT_IP: entryIp,
      NUXT_TGBOT_SECRET: password,
    },
    stdio: 'ignore',
  })
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
    const login = await api('/api/auth/sign-in/username', { username: 'admin', password })
    assert.equal(login.status, 200)
    cookie = login.headers.getSetCookie().map(c => c.split(';')[0]).join('; ')
    const created = await api('/api/clients', { name: `e2e-wg-${entryIp}` })
    assert.equal(created.status, 200)
    const client = await created.json()
    const added = await api(`/api/clients/${client.id}/devices`, { name: 'phone' })
    assert.equal(added.status, 200)
    const device = await added.json()
    const { DatabaseSync } = await import('node:sqlite')
    const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite')
    db.prepare('UPDATE clients SET tg_chat_id = ? WHERE id = ?').run(client.id, client.id)
    db.close()
    const prefix = `/api/clients/${client.id}/devices/${device.id}`
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
          assert.match(body, /^Endpoint = 203\.0\.113\.7:51820$/m)
          assert(Buffer.byteLength(body) <= 2953)
        }
      }
    }
    console.log(`WG delivery ${entryIp.includes(':') ? 'without IPv4: all four APIs return actionable 422' : 'with IPv4: config, QR, bot config and send generation pass'}`)
  }
  finally {
    child.kill('SIGTERM')
    const timer = setTimeout(() => child.kill('SIGKILL'), 5000)
    await stopped.finally(() => clearTimeout(timer))
  }
}
