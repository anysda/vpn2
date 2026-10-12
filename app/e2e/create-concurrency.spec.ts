import { expect, loginApi, test, uniq } from './support/fixtures'
import { container, inside } from './support/openvpn-ca'

test('параллельное создание и повтор запроса не дублируют клиента и устройство', async ({ page }) => {
  await loginApi(page)
  const name = uniq('create-race')
  const created = await Promise.all(Array.from({ length: 16 }, () =>
    page.request.post('/api/clients', { data: { name } }),
  ))
  expect(created.map(r => r.status()).sort()).toEqual([200, ...Array(15).fill(409)])
  const client = await created.find(r => r.status() === 200)!.json()
  const url = `/api/clients/${client.id}`
  try {
    expect((await page.request.post('/api/clients', { data: { name } })).status()).toBe(409)
    const added = await Promise.all(Array.from({ length: 16 }, () =>
      page.request.post(`${url}/devices`, { data: { name: 'phone' } }),
    ))
    expect(added.map(r => r.status()).sort()).toEqual([200, ...Array(15).fill(409)])
    expect((await page.request.post(`${url}/devices`, { data: { name: 'phone' } })).status()).toBe(409)
    const after = await (await page.request.get(url)).json()
    expect(after.devices).toHaveLength(1)
    console.log('create/retry: clients 1 accepted + 15 conflicts; devices 1 accepted + 15 conflicts; both retries 409')
  }
  finally {
    await page.request.delete(url)
  }
})

test('параллельная выдача адресов всех протоколов не создаёт коллизий', async ({ page }) => {
  test.skip(!container, 'чтение SQLite разрешено только в одноразовом e2e-контейнере')
  await loginApi(page)
  const created = await page.request.post('/api/clients', { data: { name: uniq('ip-race'), deviceLimit: null } })
  expect(created.status()).toBe(200)
  const client = await created.json()
  const url = `/api/clients/${client.id}`
  try {
    const added = await Promise.all(Array.from({ length: 16 }, (_, i) =>
      page.request.post(`${url}/devices`, { data: { name: `phone-${i}` } }),
    ))
    expect(added.map(r => r.status())).toEqual(Array(16).fill(200))
    const rows: Array<Record<string, string | null>> = JSON.parse(inside(`
      import { DatabaseSync } from 'node:sqlite';
      const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite');
      console.log(JSON.stringify(db.prepare('SELECT wg_ip, ovpn_ip, ikev2_ip, ikev2_username FROM devices WHERE client_id = ?').all(JSON.parse(process.argv[1]))));
      db.close();
    `, client.id))
    expect(rows).toHaveLength(16)
    for (const field of ['wg_ip', 'ovpn_ip', 'ikev2_ip', 'ikev2_username']) {
      const values = rows.map(row => row[field])
      expect(values.every(Boolean), field).toBe(true)
      expect(new Set(values).size, field).toBe(16)
    }
    console.log('address claims: 16 accepted; 16 unique WG/OVPN/IKEv2 addresses and IKEv2 usernames')
  }
  finally {
    await page.request.delete(url)
  }
})

test('параллельные новые устройства соблюдают лимит клиента', async ({ page }) => {
  await loginApi(page)
  const created = await page.request.post('/api/clients', { data: { name: uniq('create-limit'), deviceLimit: 1 } })
  expect(created.status()).toBe(200)
  const client = await created.json()
  const url = `/api/clients/${client.id}`
  try {
    const added = await Promise.all(Array.from({ length: 16 }, (_, i) =>
      page.request.post(`${url}/devices`, { data: { name: `phone-${i}` } }),
    ))
    expect(added.map(r => r.status()).sort()).toEqual([200, ...Array(15).fill(409)])
    expect((await (await page.request.get(url)).json()).devices).toHaveLength(1)
    console.log('create/limit: 1 accepted + 15 conflicts; devices=1, limit=1')
  }
  finally {
    await page.request.delete(url)
  }
})
