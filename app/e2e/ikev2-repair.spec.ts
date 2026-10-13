import { test, expect } from '@playwright/test'
import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import * as schema from '../server/database/schema'
import { nextPoolAddress } from '../server/utils/address-pools'
import { coalesceRuns } from '../server/utils/coalesce'
import { slugify } from '../server/utils/naming'
import { serverModule } from './support/server-module'

test('массовое восстановление IKEv2 сохраняет креды и читает пул один раз', async () => {
  const client = createClient({ url: ':memory:' })
  const queries: string[] = []
  const db = drizzle(client, { schema, logger: { logQuery: query => queries.push(query) } })
  try {
    await migrate(db, { migrationsFolder: fileURLToPath(new URL('../server/database/migrations', import.meta.url)) })
    const [owner] = await db.insert(schema.clients).values({ name: 'repair', password: 'test' }).returning()
    const ips: string[] = []
    for (let i = 0; i < 2000; i++) ips.push(nextPoolAddress('ikev2', ips))
    await db.insert(schema.devices).values(ips.map((ip, i) => ({
      clientId: owner!.id, name: `device-${i}`, ikev2Ip: ip,
      ikev2Username: i === 0 ? 'preserved' : null,
      ikev2Password: i === 0 ? 'preserved-password' : null,
    })))
    let config = ''
    const ikev2 = serverModule<typeof import('../server/utils/ikev2')>(
      'server/utils/ikev2.ts', {
        '../database/client': { useDb: () => db }, '../database/schema': schema,
        './address-pools': { nextPoolAddress }, './naming': { slugify },
        './coalesce': { coalesceRuns }, './client-status': { isClientActive: () => true },
        './client-dns': { clientDns: () => ['192.0.2.53'] },
        'node:path': { default: path },
        'node:fs': { promises: {
          readFile: async () => 'self-signed', access: async () => {},
          mkdir: async () => {}, rename: async () => {},
          writeFile: async (_path: string, body: string) => { config = body },
        } },
        'node:child_process': { execFile: (...args: unknown[]) => {
          const callback = args.at(-1) as (error: Error | null, output: string) => void
          callback(null, '')
        } },
      }, { useLogger: () => ({ debug: () => {}, warn: () => {} }), process },
    )
    queries.length = 0
    const start = performance.now()
    await ikev2.syncIkev2()
    const reads = queries.filter(query => /^select /i.test(query)).length
    console.log(`IKEv2 repair: devices=2000 selects=${reads} ms=${Math.round(performance.now() - start)}`)
    expect(reads, 'один снимок вместо повторного чтения всего пула на каждое устройство').toBeLessThanOrEqual(3)
    const rows = await db.select().from(schema.devices)
    expect(new Set(rows.map(row => row.ikev2Username)).size).toBe(2000)
    expect(rows.every(row => row.ikev2Password)).toBe(true)
    expect(rows.map(row => row.ikev2Ip)).toEqual(ips)
    expect(rows[0]).toMatchObject({ ikev2Username: 'preserved', ikev2Password: 'preserved-password' })
    expect(config.match(/eap_id =/g)).toHaveLength(2000)
    await ikev2.syncIkev2()
    expect(await db.select().from(schema.devices)).toEqual(rows)
  }
  finally { client.close() }
})
