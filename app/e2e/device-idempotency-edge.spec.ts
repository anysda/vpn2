import { X509Certificate, randomUUID } from 'node:crypto'
import type { APIRequestContext } from '@playwright/test'
import { expect, loginApi, test, uniq } from './support/fixtures'
import { waitAccess } from './support/access'
import { container, fault, inside, restore, rows } from './support/openvpn-ca'

/**
 * Края ключа идемпотентности при создании устройства: ключ переживает
 * удаление устройства, а повтор из бота не отдаёт устройство прежнего
 * владельца чата после перепривязки.
 */

async function newClient(api: APIRequestContext) {
  const r = await api.post('/api/clients', { data: { name: uniq('idem'), deviceLimit: null } })
  expect(r.status(), await r.text()).toBe(200)
  return (await r.json()).id as number
}

async function devices(api: APIRequestContext, clientId: number) {
  const r = await api.get(`/api/clients/${clientId}`)
  expect(r.status(), await r.text()).toBe(200)
  return (await r.json()).devices as { id: number, name: string }[]
}

type BotReply = { status: number, body: { devices?: { id: number, name: string }[], statusMessage?: string } }

function bot(path: string, payload: unknown, key?: string): BotReply {
  return JSON.parse(inside(`
    const [path, payload, key] = process.argv.slice(1).map(JSON.parse);
    const headers = { authorization: 'Bearer ' + process.env.NUXT_TGBOT_SECRET, 'content-type': 'application/json' };
    if (key) headers['idempotency-key'] = key;
    const r = await fetch('http://127.0.0.1:31821' + path, { method: 'POST', headers, body: JSON.stringify(payload) });
    console.log(JSON.stringify({ status: r.status, body: await r.json() }));
  `, path, payload, key ?? null)) as BotReply
}

// Инвайт клиента — ?start=<password>; панель отдаёт пароль только в Telegram.
function linkChat(clientId: number, chatId: number) {
  const password = inside(`
    import { DatabaseSync } from 'node:sqlite';
    const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite'); db.exec('PRAGMA busy_timeout=10000');
    console.log(db.prepare('SELECT password FROM clients WHERE id=?').get(JSON.parse(process.argv[1])).password);
  `, clientId)
  const r = bot('/api/bot/link', { password, chatId })
  expect(r.status, JSON.stringify(r.body)).toBe(200)
}

test.describe('ключ идемпотентности создания устройства: края', () => {
  const created: number[] = []

  test.beforeEach(async ({ page }) => {
    await loginApi(page)
  })

  test.afterEach(async ({ page }) => {
    for (const id of created.splice(0)) await page.request.delete(`/api/clients/${id}`)
  })

  test('панель: повтор ключа после удаления устройства не создаёт его заново', async ({ page }) => {
    const clientId = await newClient(page.request)
    created.push(clientId)
    const key = randomUUID()
    const add = () => page.request.post(`/api/clients/${clientId}/devices`, {
      data: { name: 'phone' },
      headers: { 'idempotency-key': key },
    })

    const first = await add()
    expect(first.status(), await first.text()).toBe(200)
    const deviceId = (await first.json()).id as number

    const removal = await page.request.delete(`/api/clients/${clientId}/devices/${deviceId}`)
    expect(removal.status(), await removal.text()).toBe(202)
    await waitAccess(page.request, (await removal.json()).operation)
    await expect.poll(async () => (await devices(page.request, clientId)).map(d => d.id)).not.toContain(deviceId)

    const again = await add()
    expect(again.status(), await again.text()).toBeGreaterThanOrEqual(400)
    expect(again.status()).toBeLessThan(500)
    expect(await devices(page.request, clientId)).toHaveLength(0)
  })

  test('бот: повтор ключа после перепривязки чата не отдаёт устройство прежнего клиента', async ({ page }) => {
    test.skip(!container, 'loopback ручки бота есть только в одноразовом контейнере run-local.sh')
    const a = await newClient(page.request)
    created.push(a)
    const b = await newClient(page.request)
    created.push(b)
    const chatId = 900000 + a
    const key = randomUUID()

    linkChat(a, chatId)
    const first = bot('/api/bot/devices', { chatId, name: 'phone' }, key)
    expect(first.status, JSON.stringify(first.body)).toBe(200)
    const phoneOfA = (await devices(page.request, a)).find(d => d.name === 'phone')
    expect(phoneOfA).toBeDefined()

    // Панель: «Отозвать доступ к боту» у A, затем чат входит по инвайту B.
    const unlink = await page.request.post(`/api/clients/${a}/unlink`)
    expect(unlink.status(), await unlink.text()).toBe(200)
    linkChat(b, chatId)

    const again = bot('/api/bot/devices', { chatId, name: 'phone' }, key)
    expect(again.status, JSON.stringify(again.body)).toBeGreaterThanOrEqual(400)
    expect(again.status).toBeLessThan(500)
    expect((await devices(page.request, b)).map(d => d.id)).not.toContain(phoneOfA!.id)
  })

  test('панель: повтор ключа после удаления клиента — 410 device_deleted', async ({ page }) => {
    const clientId = await newClient(page.request)
    created.push(clientId)
    const key = randomUUID()
    const add = () => page.request.post(`/api/clients/${clientId}/devices`, {
      data: { name: 'phone' },
      headers: { 'idempotency-key': key },
    })

    const first = await add()
    expect(first.status(), await first.text()).toBe(200)
    await removeClient(page.request, clientId)
    const before = await everyone(page.request)

    const again = await add()
    expect(again.status(), await again.text()).toBe(410)
    expect((await again.json()).statusMessage).toBe('device_deleted')
    expect(await everyone(page.request)).toEqual(before)
    expect((await page.request.get(`/api/clients/${clientId}`)).status()).toBe(404)
  })

  test('бот: повтор ключа после удаления клиента — 410 device_deleted', async ({ page }) => {
    test.skip(!container, 'loopback ручки бота есть только в одноразовом контейнере run-local.sh')
    const clientId = await newClient(page.request)
    created.push(clientId)
    const chatId = 910000 + clientId
    const key = randomUUID()

    linkChat(clientId, chatId)
    const first = bot('/api/bot/devices', { chatId, name: 'phone' }, key)
    expect(first.status, JSON.stringify(first.body)).toBe(200)
    await removeClient(page.request, clientId)
    const before = await everyone(page.request)

    const again = bot('/api/bot/devices', { chatId, name: 'phone' }, key)
    expect(again.status, JSON.stringify(again.body)).toBe(410)
    expect(again.body.statusMessage).toBe('device_deleted')
    expect(await everyone(page.request)).toEqual(before)
  })

  test('бот: повтор ключа из отвязанного чата при живом клиенте — 404 not_linked без устройства', async ({ page }) => {
    test.skip(!container, 'loopback ручки бота есть только в одноразовом контейнере run-local.sh')
    const clientId = await newClient(page.request)
    created.push(clientId)
    const chatId = 920000 + clientId
    const key = randomUUID()
    const name = uniq('phone')

    linkChat(clientId, chatId)
    const first = bot('/api/bot/devices', { chatId, name }, key)
    expect(first.status, JSON.stringify(first.body)).toBe(200)
    const [phone] = await devices(page.request, clientId)
    expect(phone?.name).toBe(name)
    const unlink = await page.request.post(`/api/clients/${clientId}/unlink`)
    expect(unlink.status(), await unlink.text()).toBe(200)

    const again = bot('/api/bot/devices', { chatId, name }, key)
    expect(again.status, JSON.stringify(again.body)).toBe(404)
    expect(again.body.statusMessage).toBe('not_linked')
    expect(again.body.devices).toBeUndefined()
    expect(JSON.stringify(again.body)).not.toContain(name)
    expect((await devices(page.request, clientId)).map(d => d.id)).toEqual([phone!.id])
  })

  test('панель: незнакомый ключ для удалённого клиента — 404 not_found', async ({ page }) => {
    const clientId = await newClient(page.request)
    created.push(clientId)
    await removeClient(page.request, clientId)
    const before = await everyone(page.request)

    const r = await page.request.post(`/api/clients/${clientId}/devices`, {
      data: { name: 'phone' },
      headers: { 'idempotency-key': randomUUID() },
    })
    expect(r.status(), await r.text()).toBe(404)
    expect((await r.json()).statusMessage).toBe('not_found')
    expect(await everyone(page.request)).toEqual(before)
  })

  test('повтор ключа, пока клиент удаляется, — 410 device_deleted в панели и в боте', async ({ page }) => {
    test.setTimeout(180_000)
    test.skip(!container, 'подмена CA разрешена только в одноразовом контейнере')
    const clientId = await newClient(page.request)
    created.push(clientId)
    const chatId = 930000 + clientId
    const panelKey = randomUUID()
    const botKey = randomUUID()
    const panelAdd = () => page.request.post(`/api/clients/${clientId}/devices`, {
      data: { name: 'laptop' },
      headers: { 'idempotency-key': panelKey },
    })

    linkChat(clientId, chatId)
    expect((await panelAdd()).status()).toBe(200)
    const fromBot = bot('/api/bot/devices', { chatId, name: 'phone' }, botKey)
    expect(fromBot.status, JSON.stringify(fromBot.body)).toBe(200)
    const saved = rows(clientId)
    expect(saved).toHaveLength(2)
    try {
      // Отзыв OpenVPN не проходит: клиент остаётся в состоянии «удаляется».
      fault('revoke', new X509Certificate(saved[0]!.ovpn_cert).serialNumber)
      const removal = await page.request.delete(`/api/clients/${clientId}`)
      expect(removal.status(), await removal.text()).toBe(202)
      const { operation } = await removal.json()
      await expect.poll(async () => (await (await page.request.get(`/api/access-operations/${operation.id}`)).json()).protocols.openvpn.state, { timeout: 30_000 }).toBe('failed')
      const detail = await page.request.get(`/api/clients/${clientId}`)
      expect(detail.status(), await detail.text()).toBe(200)
      expect((await detail.json()).deleting).toBe(true)

      const panelAgain = await panelAdd()
      expect(panelAgain.status(), await panelAgain.text()).toBe(410)
      expect((await panelAgain.json()).statusMessage).toBe('device_deleted')
      const botAgain = bot('/api/bot/devices', { chatId, name: 'phone' }, botKey)
      expect(botAgain.status, JSON.stringify(botAgain.body)).toBe(410)
      expect(botAgain.body.statusMessage).toBe('device_deleted')
      expect(rows(clientId).map(d => d.id)).toEqual(saved.map(d => d.id))

      restore()
      await page.request.post(`/api/access-operations/${operation.id}/retry`)
      await waitAccess(page.request, operation)
    }
    finally {
      restore()
    }
  })
})

async function removeClient(api: APIRequestContext, clientId: number) {
  const removal = await api.delete(`/api/clients/${clientId}`)
  expect(removal.status(), await removal.text()).toBe(202)
  await waitAccess(api, (await removal.json()).operation)
  await expect.poll(async () => (await api.get(`/api/clients/${clientId}`)).status()).toBe(404)
}

// Все клиенты и число их устройств: повтор не должен ничего создать.
async function everyone(api: APIRequestContext) {
  const r = await api.get('/api/clients')
  expect(r.status(), await r.text()).toBe(200)
  return ((await r.json()) as { id: number, deviceCount: number }[])
    .map(c => [c.id, c.deviceCount]).sort((a, b) => a[0]! - b[0]!)
}
