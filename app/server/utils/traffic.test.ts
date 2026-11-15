import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { fileURLToPath } from 'node:url'
import { expect, it, vi } from 'vitest'
import * as schema from '../database/schema'

const state = vi.hoisted(() => ({ db: null as unknown, samples: {} as Record<string, unknown[]> }))
vi.mock('../database/client', () => ({ useDb: () => state.db }))
vi.mock('./control', async importOriginal => ({
  ...await importOriginal<typeof import('./control')>(),
  control: async (_method: string, { protocol }: { protocol: string }) => {
    if (!(protocol in state.samples)) throw new Error('control down')
    return { samples: state.samples[protocol] }
  },
}))
vi.stubGlobal('useLogger', () => ({ info: () => {}, warn: () => {}, error: () => {} }))

it('сборщик: прирост счётчиков WG, OpenVPN и IKEv2, сброс счётчика и новая IKE_SA', async () => {
  const db = drizzle(createClient({ url: ':memory:' }), { schema })
  await migrate(db, { migrationsFolder: fileURLToPath(new URL('../database/migrations', import.meta.url)) })
  state.db = db
  await db.insert(schema.clients).values({ id: 1, name: 'a', password: 'p' })
  await db.insert(schema.devices).values([
    { id: 1, clientId: 1, name: 'phone', wgPublicKey: 'K1', ikev2Username: 'u1' },
    { id: 2, clientId: 1, name: 'laptop' },
  ])
  const { collectTraffic, recentBytesByDevice } = await import('./traffic')

  state.samples = {
    wg: [{ key: 'K1', rx: 100, tx: 50 }, { key: 'unknown', rx: 9, tx: 9 }],
    openvpn: [{ device_id: 2, rx: 10, tx: 10 }],
    ikev2: [{ username: 'u1', uniqueid: 'a', rx: 5, tx: 5 }],
  }
  await collectTraffic()
  const totals = async () => (await db.select({ id: schema.devices.id, rx: schema.devices.rxTotal, tx: schema.devices.txTotal })
    .from(schema.devices).orderBy(schema.devices.id))
  expect(await totals()).toEqual([{ id: 1, rx: 0, tx: 0 }, { id: 2, rx: 0, tx: 0 }])

  state.samples = {
    wg: [{ key: 'K1', rx: 300, tx: 80 }],
    openvpn: [{ device_id: 2, rx: 5, tx: 20 }],
    ikev2: [{ username: 'u1', uniqueid: 'a', rx: 15, tx: 5 }, { username: 'u1', uniqueid: 'b', rx: 7, tx: 3 }],
  }
  await collectTraffic()
  expect(await totals()).toEqual([{ id: 1, rx: 217, tx: 33 }, { id: 2, rx: 5, tx: 10 }])
  const hourly = await db.select().from(schema.deviceTrafficHourly).orderBy(schema.deviceTrafficHourly.deviceId)
  expect(hourly.map(h => [h.deviceId, h.rx, h.tx])).toEqual([[1, 217, 33], [2, 5, 10]])
  const snapshots = Object.fromEntries((await db.select().from(schema.collectorState)).map(r => [r.name, JSON.parse(r.value)]))
  expect(snapshots).toEqual({ wg: { '1:wg': [300, 80] }, openvpn: { '2:openvpn': [5, 20] }, ikev2: { '1:a': [15, 5], '1:b': [7, 3] } })
  expect([...recentBytesByDevice()]).toEqual([[1, 250], [2, 15]])

  // Рестарт панели: новый процесс берёт прошлый снимок из базы, прирост за простой засчитан.
  vi.resetModules()
  const restarted = await import('./traffic')
  state.samples = { wg: [{ key: 'K1', rx: 1300, tx: 580 }], openvpn: [{ device_id: 2, rx: 105, tx: 70 }], ikev2: [] }
  await restarted.collectTraffic()
  expect(await totals()).toEqual([{ id: 1, rx: 1217, tx: 533 }, { id: 2, rx: 105, tx: 60 }])

  // Протокол без ответа сохраняет прошлый снимок: прирост засчитывает следующий прогон.
  state.samples = { openvpn: [{ device_id: 2, rx: 105, tx: 70 }], ikev2: [] }
  await restarted.collectTraffic()
  state.samples = { wg: [{ key: 'K1', rx: 1400, tx: 600 }], openvpn: [{ device_id: 2, rx: 105, tx: 70 }], ikev2: [] }
  await restarted.collectTraffic()
  expect(await totals()).toEqual([{ id: 1, rx: 1317, tx: 553 }, { id: 2, rx: 105, tx: 60 }])
})
