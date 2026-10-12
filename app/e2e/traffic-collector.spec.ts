import { test, expect } from '@playwright/test'
import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { fileURLToPath } from 'node:url'
import * as schema from '../server/database/schema'
import { ikev2Deltas } from '../server/utils/ikev2-counters'
import { coalesceRuns } from '../server/utils/coalesce'
import { serverModule } from './support/server-module'

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
        }, { useLogger: () => ({ info: () => {} }) },
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
