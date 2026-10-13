import { expect, loginApi, test, uniq, toast } from './support/fixtures'
import { openClient } from './support/clients'
import { container, inside, file } from './support/openvpn-ca'
import { buildWgClientConfig } from '../server/utils/wireguard'

const protocols = [
  { key: 'wg', name: 'WireGuard', octet: 66, last: 254 },
  { key: 'ovpn', name: 'OpenVPN', octet: 67, last: 239 },
  { key: 'ikev2', name: 'IKEv2', octet: 68, last: 254 },
] as const

function addresses(octet: number, last: number, legacy = false) {
  const subnets = legacy ? [octet] : [octet, ...Array.from({ length: 8 }, (_, i) => 64 + i).filter(i => i !== octet)]
  return subnets.flatMap((n, index) => Array.from({ length: (index ? 254 : last) - 1 }, (_, i) => `10.${octet}.${n}.${i + 2}`)).slice(0, 2000)
}

function database(code: string, ...args: unknown[]) {
  return inside(`import { DatabaseSync } from 'node:sqlite';
    const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
    db.exec('PRAGMA busy_timeout=5000');
    const args = process.argv.slice(1).map(JSON.parse);
    ${code}`, ...args)
}

test('2000 WireGuard IPv6-адресов уникальны, старая /24 сохраняет ULA', () => {
  const ula = addresses(66, 254).map(clientIp => {
    const config = buildWgClientConfig({ clientIp, clientPrivateKey: 'test', clientPresharedKey: 'test', serverPublicKey: 'test', serverEndpoint: 'vpn.example.test', serverPort: 51820 })
    return /^Address = .*?, (.*)\/64$/m.exec(config)![1]!
  })
  expect(new Set(ula).size).toBe(2000)
  expect(ula.slice(0, 253)).toEqual(Array.from({ length: 253 }, (_, i) => `fd66:66::${i + 2}`))
})

for (const protocol of protocols) {
  test(`${protocol.name}: исчерпание → 409 без строки, освобождение → полная выдача`, async ({ page, guard }) => {
    test.skip(!container)
    await loginApi(page)
    const name = uniq('pool')
    const created = await page.request.post('/api/clients', { data: { name, deviceLimit: null } })
    expect(created.status()).toBe(200)
    const { id } = await created.json()
    const pool = addresses(protocol.octet, protocol.last)
    try {
      database(`const [id, key, pool] = args;
        const add = db.prepare('INSERT OR IGNORE INTO devices (client_id, name, ' + key + '_ip) VALUES (?, ?, ?)');
        db.exec('BEGIN');
        for (const ip of pool) add.run(id, 'occupied-' + ip, ip);
        db.prepare('UPDATE clients SET tg_chat_id = ? WHERE id = ?').run(700000 + id, id);
        db.exec('COMMIT');`, id, protocol.key, pool)
      const before = database('console.log(db.prepare("SELECT count(*) AS n FROM devices").get().n);')
      const message = `Нет свободных адресов ${protocol.name}. Удалите неиспользуемое устройство или обратитесь к администратору`
      const response = await page.request.post(`/api/clients/${id}/devices`, { data: { name: 'overflow' } })
      console.log(`${protocol.name}: create HTTP ${response.status()}, rows before=${before}, after=${database('console.log(db.prepare("SELECT count(*) AS n FROM devices").get().n);')}`)
      expect(response.status()).toBe(409)
      expect((await response.json()).message).toBe(message)
      const bot = JSON.parse(inside(`const response = await fetch('http://127.0.0.1:51821/api/bot/devices', {
        method: 'POST', headers: { authorization: 'Bearer ' + process.env.NUXT_TGBOT_SECRET, 'Content-Type': 'application/json' },
        body: JSON.stringify({ chatId: 700000 + JSON.parse(process.argv[1]), name: 'bot-overflow' }) });
        console.log(JSON.stringify({ status: response.status, body: await response.json() }));`, id))
      expect(bot.status, JSON.stringify(bot.body)).toBe(409)
      expect(bot.body.message).toBe(message)
      // В браузере открываем отдельного пустого клиента, чтобы не рендерить 2000 карточек.
      const empty = await page.request.post('/api/clients', { data: { name: `${name}-ui`, deviceLimit: null } })
      const uiClient = await empty.json()
      try {
        guard.allow(409, /\/devices$/, 'POST')
        await page.goto('/')
        await openClient(page, `${name}-ui`)
        await page.getByRole('button', { name: 'девайс', exact: true }).click()
        const dialog = page.getByRole('dialog', { name: 'Новый девайс' })
        await dialog.getByPlaceholder('напр. iPhone').fill('ui-overflow')
        await dialog.getByRole('button', { name: 'Добавить', exact: true }).click()
        await expect(toast(page, message)).toBeVisible()
        await expect(dialog).toBeVisible()
      }
      finally {
        await page.request.delete(`/api/clients/${uiClient.id}`)
      }
      expect(database('console.log(db.prepare("SELECT count(*) AS n FROM devices").get().n);')).toBe(before)
      database('db.prepare("DELETE FROM devices WHERE client_id = ? AND name = ?").run(args[0], args[1]);', id, `occupied-${pool.at(-1)}`)
      const freed = await page.request.post(`/api/clients/${id}/devices`, { data: { name: 'freed' } })
      expect(freed.status(), await freed.text()).toBe(200)
      const device = await freed.json()
      const row = JSON.parse(database('console.log(JSON.stringify(db.prepare("SELECT * FROM devices WHERE id = ?").get(args[0])));', device.id))
      for (const key of ['wg_ip', 'wg_private_key', 'ovpn_ip', 'ovpn_cert', 'ikev2_ip', 'ikev2_password']) expect(row[key], key).toBeTruthy()
      expect(row[`${protocol.key}_ip`]).toBe(pool.at(-1))
    }
    finally {
      database('db.prepare("DELETE FROM devices WHERE client_id = ? AND name LIKE ?").run(args[0], "occupied-%");', id)
      await page.request.delete(`/api/clients/${id}`)
    }
  })
}

test('миграция занятой /24: прежние ключи и конфиги неизменны, новые адреса во всех протоколах', async ({ page }) => {
  test.skip(!container)
  await loginApi(page)
  const created = await page.request.post('/api/clients', { data: { name: uniq('migration'), deviceLimit: null } })
  const { id } = await created.json()
  try {
    const original = await page.request.post(`/api/clients/${id}/devices`, { data: { name: 'original' } })
    expect(original.status()).toBe(200)
    const device = await original.json()
    const read = async (suffix: string) => {
      const response = await page.request.get(`/api/clients/${id}/devices/${device.id}/${suffix}`)
      expect(response.status()).toBe(200)
      return response.text()
    }
    const before = await Promise.all(['wg-config', 'ovpn-config', 'ikev2-credentials'].map(read))
    const ccd = file(`/etc/openvpn/server/ccd/dev_${device.id}`)
    const row = database('console.log(JSON.stringify(db.prepare("SELECT * FROM devices WHERE id = ?").get(args[0])));', device.id)
    for (const p of protocols) database(`const [id, key, pool] = args;
      const add = db.prepare('INSERT OR IGNORE INTO devices (client_id, name, ' + key + '_ip) VALUES (?, ?, ?)');
      db.exec('BEGIN');
      for (const ip of pool) add.run(id, 'occupied-' + ip, ip);
      db.exec('COMMIT');`, id, p.key, addresses(p.octet, p.last, true))
    const next = await page.request.post(`/api/clients/${id}/devices`, { data: { name: 'expanded' } })
    expect(next.status(), await next.text()).toBe(200)
    const added = await next.json()
    const expanded = JSON.parse(database('console.log(JSON.stringify(db.prepare("SELECT * FROM devices WHERE id = ?").get(args[0])));', added.id))
    for (const p of protocols) expect(expanded[`${p.key}_ip`]).toBe(`10.${p.octet}.64.2`)
    expect(await Promise.all(['wg-config', 'ovpn-config', 'ikev2-credentials'].map(read))).toEqual(before)
    expect(database('console.log(JSON.stringify(db.prepare("SELECT * FROM devices WHERE id = ?").get(args[0])));', device.id)).toBe(row)
    expect(file(`/etc/openvpn/server/ccd/dev_${device.id}`)).toBe(ccd)
    expect(file(`/etc/openvpn/server/ccd/dev_${added.id}`)).toContain('ifconfig-push 10.67.64.2 255.255.248.0')
    expect(file('/etc/wireguard/wg0.conf')).toContain('AllowedIPs = 10.66.64.2/32, fd66:66::64:2/128')
    expect(file('/etc/swanctl/conf.d/anysda-clients.conf')).toContain('addrs = 10.68.64.2-10.68.64.2')
  }
  finally {
    database('db.prepare("DELETE FROM devices WHERE client_id = ? AND name LIKE ?").run(args[0], "occupied-%");', id)
    await page.request.delete(`/api/clients/${id}`)
  }
})

test('два параллельных создания на последний адрес: один успех, один 409', async ({ page }) => {
  test.skip(!container)
  await loginApi(page)
  const created = await page.request.post('/api/clients', { data: { name: uniq('last-ip'), deviceLimit: null } })
  const { id } = await created.json()
  try {
    database(`const [id, pools] = args;
      const insert = db.prepare('INSERT INTO devices (client_id, name, wg_ip, ovpn_ip, ikev2_ip) VALUES (?, ?, ?, ?, ?)');
      db.exec('BEGIN');
      for (let i = 0; i < 1999; i++) insert.run(id, 'occupied-' + i, ...pools.map(pool => pool[i]));
      db.exec('COMMIT');`, id, protocols.map(p => addresses(p.octet, p.last)))
    const responses = await Promise.all(['first', 'second'].map(name => page.request.post(`/api/clients/${id}/devices`, { data: { name } })))
    expect(responses.map(r => r.status()).sort()).toEqual([200, 409])
    expect(database('console.log(db.prepare("SELECT count(*) AS n FROM devices WHERE client_id = ?").get(args[0]).n);', id)).toBe('2000')
    const row = JSON.parse(database('console.log(JSON.stringify(db.prepare("SELECT * FROM devices WHERE client_id = ? AND name NOT LIKE ?").get(args[0], "occupied-%")));', id))
    for (const p of protocols) expect(row[`${p.key}_ip`]).toBe(addresses(p.octet, p.last).at(-1))
    for (const key of ['wg_private_key', 'ovpn_cert', 'ikev2_password']) expect(row[key]).toBeTruthy()
  }
  finally {
    database('db.prepare("DELETE FROM devices WHERE client_id = ? AND name LIKE ?").run(args[0], "occupied-%");', id)
    await page.request.delete(`/api/clients/${id}`)
  }
})
