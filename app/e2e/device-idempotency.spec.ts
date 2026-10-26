import { randomUUID } from 'node:crypto'
import type { APIRequestContext } from '@playwright/test'
import { expect, loginApi, test, uniq } from './support/fixtures'
import { container, inside } from './support/openvpn-ca'

/**
 * Создание устройства принимает Idempotency-Key, как операции доступа:
 * повтор после обрыва с тем же ключом возвращает то же устройство, тот же
 * ключ с другим телом отклоняется. Без ключа повтор упирается в имя (409).
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

function addDevice(api: APIRequestContext, clientId: number, name: string, key?: string) {
  return api.post(`/api/clients/${clientId}/devices`, {
    data: { name },
    headers: key ? { 'idempotency-key': key } : {},
  })
}

test.describe('ключ идемпотентности при создании устройства', () => {
  let clientId: number

  test.beforeEach(async ({ page }) => {
    await loginApi(page)
    clientId = await newClient(page.request)
  })

  test.afterEach(async ({ page }) => {
    await page.request.delete(`/api/clients/${clientId}`)
  })

  test('панель: повтор с тем же ключом и телом возвращает то же устройство', async ({ page }) => {
    const key = randomUUID()
    const first = await addDevice(page.request, clientId, 'phone', key)
    expect(first.status(), await first.text()).toBe(200)
    const second = await addDevice(page.request, clientId, 'phone', key)
    expect(second.status(), await second.text()).toBe(200)
    expect((await second.json()).id).toBe((await first.json()).id)
    expect((await devices(page.request, clientId)).filter(d => d.name === 'phone')).toHaveLength(1)
  })

  test('панель: тот же ключ с другим именем отклоняется', async ({ page }) => {
    const key = randomUUID()
    const first = await addDevice(page.request, clientId, 'phone', key)
    expect(first.status(), await first.text()).toBe(200)
    const other = await addDevice(page.request, clientId, 'laptop', key)
    expect(other.status(), await other.text()).toBeGreaterThanOrEqual(400)
    expect(other.status()).toBeLessThan(500)
    expect((await devices(page.request, clientId)).map(d => d.name)).toEqual(['phone'])
  })

  test('панель: без ключа повтор с тем же именем по-прежнему 409', async ({ page }) => {
    const first = await addDevice(page.request, clientId, 'phone')
    expect(first.status(), await first.text()).toBe(200)
    const again = await addDevice(page.request, clientId, 'phone')
    expect(again.status(), await again.text()).toBe(409)
    expect((await again.json()).statusMessage).toBe('device_exists')
    expect(await devices(page.request, clientId)).toHaveLength(1)
  })

  test('бот: повтор с тем же ключом и телом возвращает то же устройство', async ({ page }) => {
    test.skip(!container, 'loopback ручки бота есть только в одноразовом контейнере run-local.sh')
    const chatId = 800000 + clientId
    inside(`
      import { DatabaseSync } from 'node:sqlite';
      const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite'); db.exec('PRAGMA busy_timeout=10000');
      const [id, chat] = process.argv.slice(1).map(JSON.parse);
      db.prepare('UPDATE clients SET tg_chat_id=? WHERE id=?').run(chat, id);
    `, clientId, chatId)
    const key = randomUUID()
    const post = () => JSON.parse(inside(`
      const [chatId, key] = process.argv.slice(1).map(JSON.parse);
      const r = await fetch('http://127.0.0.1:31821/api/bot/devices', {
        method: 'POST',
        headers: { authorization: 'Bearer ' + process.env.NUXT_TGBOT_SECRET, 'content-type': 'application/json', 'idempotency-key': key },
        body: JSON.stringify({ chatId, name: 'phone' }),
      });
      console.log(JSON.stringify({ status: r.status, body: await r.json() }));
    `, chatId, key)) as { status: number, body: { devices?: { id: number, name: string }[] } }
    const first = post()
    expect(first.status, JSON.stringify(first.body)).toBe(200)
    const second = post()
    expect(second.status, JSON.stringify(second.body)).toBe(200)
    const phones = (body: typeof first.body) => (body.devices ?? []).filter(d => d.name === 'phone').map(d => d.id)
    expect(phones(first.body)).toHaveLength(1)
    expect(phones(second.body)).toEqual(phones(first.body))
    expect((await devices(page.request, clientId)).filter(d => d.name === 'phone')).toHaveLength(1)
  })
})
