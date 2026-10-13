import assert from 'node:assert/strict'
import { readFileSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { DatabaseSync } from 'node:sqlite'
import { setTimeout } from 'node:timers/promises'

const origin = 'http://127.0.0.1:31821'
let cookie
async function request(path, data) {
  const response = await fetch(origin + path, {
    method: data ? 'POST' : 'GET',
    headers: { origin, 'Content-Type': 'application/json', ...(cookie ? { cookie } : {}) },
    ...(data ? { body: JSON.stringify(data) } : {}),
  })
  assert.equal(response.status, 200, `${path}: HTTP ${response.status}`)
  return response
}
for (let i = 0; ; i++) {
  try {
    const login = await request('/api/auth/sign-in/username', { username: 'admin', password: process.env.NUXT_ADMIN_PASSWORD })
    cookie = login.headers.getSetCookie().map(value => value.split(';')[0]).join('; ')
    break
  }
  catch (err) {
    if (i === 60) throw err
    await setTimeout(500)
  }
}
const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite')
db.exec('PRAGMA busy_timeout = 10000')
const snapshotFile = '/var/lib/anysda-vpn2/pool-upgrade.json'
const hash = text => createHash('sha256').update(text).digest('hex')
async function snapshot(clientId, deviceId) {
  const configs = {}
  for (const type of ['wg-config', 'ovpn-config', 'ikev2-credentials']) {
    configs[type] = hash(await (await request(`/api/clients/${clientId}/devices/${deviceId}/${type}`)).text())
  }
  return {
    rowHash: hash(JSON.stringify(db.prepare('SELECT * FROM devices WHERE id = ?').get(deviceId))),
    configs,
    ccd: readFileSync(`/etc/openvpn/server/ccd/dev_${deviceId}`, 'utf8'),
  }
}
if (process.argv[2] === 'seed') {
  const client = await (await request('/api/clients', { name: 'e2e-pool-upgrade', deviceLimit: null })).json()
  const device = await (await request(`/api/clients/${client.id}/devices`, { name: 'legacy' })).json()
  const before = await snapshot(client.id, device.id)
  db.exec('BEGIN')
  for (const [key, octet, last] of [['wg', 66, 254], ['ovpn', 67, 239], ['ikev2', 68, 254]]) {
    const insert = db.prepare(`INSERT OR IGNORE INTO devices (client_id, name, ${key}_ip) VALUES (?, ?, ?)`)
    for (let i = 2; i <= last; i++) insert.run(client.id, `${key}-${i}`, `10.${octet}.${octet}.${i}`)
  }
  db.exec('COMMIT')
  writeFileSync(snapshotFile, JSON.stringify({ clientId: client.id, deviceId: device.id, before }), { mode: 0o600 })
  console.log('OLD: legacy /24 pools occupied; device, config hashes and CCD saved')
}
else {
  const { clientId, deviceId, before } = JSON.parse(readFileSync(snapshotFile, 'utf8'))
  assert.deepEqual(await snapshot(clientId, deviceId), before)
  const added = await (await request(`/api/clients/${clientId}/devices`, { name: 'expanded' })).json()
  const row = db.prepare('SELECT * FROM devices WHERE id = ?').get(added.id)
  for (const [key, octet] of [['wg', 66], ['ovpn', 67], ['ikev2', 68]]) assert.equal(row[`${key}_ip`], `10.${octet}.64.2`)
  for (const key of ['wg_private_key', 'ovpn_cert', 'ikev2_password']) assert.ok(row[key])
  assert.deepEqual(await snapshot(clientId, deviceId), before)
  console.log('NEW: legacy row, all config hashes and CCD unchanged; WG/OpenVPN/IKEv2 allocated .64.2 with credentials')
}
db.close()
