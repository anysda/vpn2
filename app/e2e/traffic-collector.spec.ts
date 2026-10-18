import { test, expect } from '@playwright/test'
import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { fileURLToPath } from 'node:url'
import * as schema from '../server/database/schema'
import { ikev2Deltas } from '../server/utils/ikev2-counters'
import { coalesceRuns } from '../server/utils/coalesce'
import { serverModule } from './support/server-module'

const mskDay = serverModule<typeof import('../server/utils/msk-day')>(new URL('../server/utils/msk-day.ts', import.meta.url), {})

for (const proto of ['wg', 'ovpn']) {
  test(`сборщик ${proto}: ошибка записи не теряет трафик, повтор не удваивает его`, async () => {
    const client = createClient({ url: ':memory:' })
    const db = drizzle(client, { schema })
    try {
      await migrate(db, { migrationsFolder: fileURLToPath(new URL('../server/database/migrations', import.meta.url)) })
      const [owner] = await db.insert(schema.clients).values({ name: 'collector-test', password: 'unused-test-password' }).returning()
      const [device] = await db.insert(schema.devices).values({ clientId: owner!.id, name: 'test', wgPublicKey: 'test-key' }).returning()
      let counter = 100
      const collector = serverModule<typeof import('../server/utils/traffic-collector')>(
        new URL('../server/utils/traffic-collector.ts', import.meta.url), {
          '../database/client': { useDb: () => db },
          '../database/schema': schema,
          './ikev2-counters': { ikev2Deltas },
          './coalesce': { coalesceRuns },
          './msk-day': mskDay,
          './control': { control: async (_method: string, params: { protocol: string }) => {
            if (params.protocol === 'ikev2') throw new Error('no IKEv2 sample')
            return { samples: params.protocol === (proto === 'ovpn' ? 'openvpn' : proto)
              ? [{ key: 'test-key', device_id: device!.id, rx: counter * 2, tx: counter }] : [] }
          } },
          'node:child_process': {
            execFile: (...args: unknown[]) => {
              const callback = args.at(-1) as (err: Error | null, output: { stdout: string }) => void
              callback(null, { stdout: proto === 'wg' ? `test-key\t${counter}\t${counter * 2}\n` : '' })
            },
          },
          'node:fs/promises': {
            readFile: async (path: string) => {
              if (path.includes('status-server')) return proto === 'ovpn' ? `CLIENT_LIST,dev_${device!.id},addr,ip,,${counter},${counter * 2}\n` : ''
              throw new Error('no IKEv2 status file')
            },
          },
        }, { useLogger: () => ({ info: () => {} }), process },
      )
      await collector.collectTraffic()
      counter = 200
      await client.execute("CREATE TRIGGER full_disk BEFORE INSERT ON device_traffic_hourly BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END")
      await expect(Promise.resolve(collector.collectTraffic())).rejects.toThrow()
      expect((await db.select().from(schema.devices))[0]!.rxTotal).toBe(0)
      await client.execute('DROP TRIGGER full_disk')
      counter = 300
      await Promise.all([collector.collectTraffic(), collector.collectTraffic(), collector.collectTraffic()])
      const [saved] = await db.select().from(schema.devices)
      expect({ rx: saved!.rxTotal, tx: saved!.txTotal }).toEqual({ rx: 400, tx: 200 })
      const [hourly] = await db.select().from(schema.deviceTrafficHourly)
      expect({ rx: hourly!.rx, tx: hourly!.tx }).toEqual({ rx: 400, tx: 200 })
      expect(collector.recentBytesByDevice().get(device!.id)).toBe(600)
    }
    finally {
      client.close()
    }
  })
}

test('история трафика: год хранится, сутки старше двух недель свёрнуты, суммы по суткам и периодам прежние', async () => {
  const client = createClient({ url: ':memory:' })
  const db = drizzle(client, { schema })
  try {
    await migrate(db, { migrationsFolder: fileURLToPath(new URL('../server/database/migrations', import.meta.url)) })
    const ids: number[] = []
    for (const name of ['history-a', 'history-b']) {
      const [owner] = await db.insert(schema.clients).values({ name, password: 'unused-test-password' }).returning()
      for (const dev of ['phone', 'laptop']) {
        const [d] = await db.insert(schema.devices).values({ clientId: owner!.id, name: dev }).returning()
        ids.push(d!.id)
      }
    }
    const nowSec = Math.floor(Date.now() / 1000)
    const hourNow = Math.floor(nowSec / 3600) * 3600
    // 360 суток каждый час (до 3 МБ за час - часть суток выше порога «активен»)
    // и двое суток за пределами года, которые срок хранения должен удалить.
    const rows: Array<typeof schema.deviceTrafficHourly.$inferInsert> = []
    for (const [i, deviceId] of ids.entries()) {
      for (let h = 0; h < 360 * 24; h++) {
        const v = ((h * 7919 + i * 104729) % 1000) * 3000
        rows.push({ deviceId, hour: hourNow - h * 3600, rx: v, tx: v >> 3 })
      }
      for (const days of [370, 371]) rows.push({ deviceId, hour: hourNow - days * 86400, rx: 5, tx: 5 })
    }
    for (let i = 0; i < rows.length; i += 2000) await db.insert(schema.deviceTrafficHourly).values(rows.slice(i, i + 2000))

    // Каждая загрузка - как свежий запуск панели: первый прогон сборщика чистит историю.
    const loadCollector = () => serverModule<typeof import('../server/utils/traffic-collector')>(
      new URL('../server/utils/traffic-collector.ts', import.meta.url), {
        '../database/client': { useDb: () => db },
        '../database/schema': schema,
        './ikev2-counters': { ikev2Deltas },
        './coalesce': { coalesceRuns },
        './msk-day': mskDay,
        './control': { control: async () => { throw new Error('no traffic sample') } },
        'node:child_process': { execFile: (...args: unknown[]) => (args.at(-1) as (e: Error | null, o: { stdout: string }) => void)(null, { stdout: '' }) },
        'node:fs/promises': { readFile: async () => { throw new Error('no status file') } },
      }, { useLogger: () => ({ info: () => {} }), process },
    )
    const collector = loadCollector()
    const activity = serverModule<typeof import('../server/utils/client-activity')>(
      new URL('../server/utils/client-activity.ts', import.meta.url), {
        '../database/client': { useDb: () => db },
        '../database/schema': schema,
        './msk-day': mskDay,
        './traffic-collector': collector,
      },
    )
    const { activityDays } = serverModule<typeof import('../server/utils/activity-days')>(
      new URL('../server/utils/activity-days.ts', import.meta.url), {
        '../database/client': { useDb: () => db },
        '../database/schema': schema,
        './client-activity': activity,
        './msk-day': mskDay,
      },
    )
    const consumers = async () => ({
      days: (await activityDays()).days,
      day: [...(await activity.periodTotalsByClient(activity.periodStartSec('day'))).entries()],
      week: [...(await activity.periodTotalsByClient(activity.periodStartSec('week'))).entries()],
    })
    const table = () => db.select().from(schema.deviceTrafficHourly).orderBy(schema.deviceTrafficHourly.deviceId, schema.deviceTrafficHourly.hour)
    const cutoff = mskDay.mskDayStartSec(nowSec * 1000, 14)
    const before = await consumers()
    const recentBefore = (await table()).filter(r => r.hour >= cutoff)

    await collector.collectTraffic()

    const after = await consumers()
    const yearAgo = mskDay.mskDay((nowSec - 365 * 86400) * 1000)
    expect(after.days).toEqual(before.days.filter(d => d.day > yearAgo))
    expect(after.days.length).toBeGreaterThan(355)
    expect(after.day).toEqual(before.day)
    expect(after.week).toEqual(before.week)
    const compacted = await table()
    // Последние две недели - по часам как были, старше - запись на сутки в 00:00 МСК.
    expect(compacted.filter(r => r.hour >= cutoff)).toEqual(recentBefore)
    const old = compacted.filter(r => r.hour < cutoff)
    expect(old.every(r => r.hour === mskDay.mskDayStartSec(r.hour * 1000))).toBe(true)
    expect(old.length).toBe(new Set(old.map(r => `${r.deviceId}:${r.hour}`)).size)
    expect(compacted.length).toBeLessThan(rows.length / 10)

    // Повтор после перезапуска на уже свёрнутой базе ничего не меняет.
    await loadCollector().collectTraffic()
    expect(await table()).toEqual(compacted)
  }
  finally {
    client.close()
  }
})
