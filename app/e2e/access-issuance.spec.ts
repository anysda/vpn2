import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'
import { and, eq, exists } from 'drizzle-orm'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { expect, test } from '@playwright/test'
import type * as Schema from '../server/database/schema'
import type * as Database from '../server/database/client'

const require = createRequire(import.meta.url)
const harness = require('./support/access-harness.cjs')
const root = process.env.VPN2_TEST_ROOT || path.resolve('..')

test('выдача сертификата не пересекает принятую заморозку', async () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'access-issue-'))
  try {
    const app = harness(root, path.join(directory, 'db.sqlite'), {})
    const schema: typeof Schema = app.load('app/server/database/schema.ts')
    const db = (app.load('app/server/database/client.ts') as typeof Database).useDb()
    await migrate(db, { migrationsFolder: path.join(root, 'app/server/database/migrations') })
    const [client] = await db.insert(schema.clients).values({ name: 'issue race', password: 'fixture' }).returning()
    const [device] = await db.insert(schema.devices).values({ clientId: client!.id, name: 'phone' }).returning()
    const source = readFileSync(path.join(root, 'app/server/utils/openvpn.ts'), 'utf8')
    const start = source.indexOf('async function ensureDeviceOvpnOnce')
    const end = source.indexOf('/** Перевыпуск OVPN', start)
    expect(start).toBeGreaterThan(0)
    expect(end).toBeGreaterThan(start)
    const code = ts.transpile(source.slice(start, end), { target: ts.ScriptTarget.ES2022 })
    const promise = runInNewContext(`${code}\nensureDeviceOvpnOnce(${device!.id})`, {
      useDb: () => db, clientsTable: schema.clients, devicesTable: schema.devices,
      and, eq, exists,
      createError: (value: { statusCode: number, message: string }) => Object.assign(new Error(value.message), value),
      isClientActive: (c: { frozenManual: boolean }) => !c.frozenManual,
      ovpnCn: (id: number) => `dev_${id}`,
      issueClientCert: async () => {
        await db.update(schema.clients).set({ frozenManual: true,
          ...(schema.clients.accessVersion ? { accessVersion: 1 } : {}),
        }).where(eq(schema.clients.id, client!.id))
        return { cert: 'new certificate', key: 'new private key' }
      },
      ensureDeviceOvpnIp: async () => (await db.select().from(schema.devices))[0],
      writeCcd: async () => {},
    })
    await expect(promise).rejects.toThrow()
    const [saved] = await db.select().from(schema.devices)
    expect(saved!.ovpnCert).toBeNull()
    expect(saved!.ovpnKey).toBeNull()
    console.log('certificate issue started → freeze accepted → stale issue rejected; no credentials published')
  }
  finally { rmSync(directory, { recursive: true, force: true }) }
})
