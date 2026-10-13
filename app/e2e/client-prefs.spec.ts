import { test, expect } from '@playwright/test'
import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as schema from '../server/database/schema'
import { serverModule } from './support/server-module'

for (const scenario of [
  { name: 'переименование', hosts: 'se1=192.0.2.2,us=192.0.2.3', expected: ['se1', 'us'] },
  { name: 'обмен тегами', hosts: 'us=192.0.2.2,se=192.0.2.3', expected: ['us', 'se'] },
  { name: 'замена адреса при том же теге', hosts: 'se=192.0.2.4,us=192.0.2.3', expected: ['se', 'us'] },
  { name: 'удаление экзита', hosts: 'us=192.0.2.3', expected: [null, 'us'] },
  { name: 'удаление и новый адрес с новым тегом', hosts: 'se1=192.0.2.4,us=192.0.2.3', expected: [null, 'us'] },
  { name: 'неоднозначный адрес', hosts: 'se1=192.0.2.2,se2=192.0.2.2,us=192.0.2.3', expected: [null, 'us'] },
  { name: 'тег constructor', hosts: 'constructor=192.0.2.2,us=192.0.2.3', expected: ['constructor', 'us'] },
  { name: 'удаление тега constructor', initial: ['constructor', 'us'], hosts: 'us=192.0.2.3', expected: [null, 'us'] },
]) {
  const initial = scenario.initial ?? ['se', 'us']
  const before = `ru=192.0.2.1,${initial[0]}=192.0.2.2,us=192.0.2.3`
  for (const upgrade of [false, true]) {
    test(`предпочтения: ${scenario.name}${upgrade ? ', обновление старой панели' : ''}`, async () => {
      const directory = await mkdtemp(join(tmpdir(), 'vpn2-client-prefs-'))
      const client = createClient({ url: ':memory:' })
      const db = drizzle(client, { schema })
      const cfg = { exitTags: initial.join(' '), nodeHosts: before, previousNodeHosts: '', clientPrefsFilePath: join(directory, 'prefs.json') }
      const logs: { oldTag: string, newTag: string | null }[] = []
      const load = () => serverModule<typeof import('../server/utils/client-prefs')>(
        new URL('../server/utils/client-prefs.ts', import.meta.url), {
          '../database/client': { useDb: () => db }, '../database/schema': schema,
        }, { process, useRuntimeConfig: () => cfg, useLogger: () => ({ info: (data: { oldTag: string, newTag: string | null }) => { if (data.oldTag) logs.push(data) } }) },
      )
      try {
        await migrate(db, { migrationsFolder: fileURLToPath(new URL('../server/database/migrations', import.meta.url)) })
        for (const [i, tag] of [...initial, null].entries()) {
          const [owner] = await db.insert(schema.clients).values({ name: `client-${i}`, password: 'unused-test-password', preferredExit: tag }).returning()
          await db.insert(schema.devices).values({ clientId: owner!.id, name: 'device', wgIp: `10.66.66.${i + 2}`, ovpnIp: `10.67.67.${i + 2}`, ikev2Ip: `10.68.68.${i + 2}` })
        }
        if (upgrade) cfg.previousNodeHosts = before
        else await load().syncClientPrefsFile()
        cfg.nodeHosts = scenario.hosts
        cfg.exitTags = scenario.hosts.split(',').map(pair => pair.split('=')[0]).join(' ')
        if (!upgrade) {
          // Ошибка сохранения карты должна откатить и переносы клиентов.
          await client.execute("CREATE TRIGGER fail_map BEFORE INSERT ON collector_state BEGIN SELECT RAISE(ABORT, 'disk is full'); END")
          await expect(Promise.resolve(load().syncClientPrefsFile())).rejects.toThrow()
          expect((await db.select().from(schema.clients).orderBy(schema.clients.id)).map(row => row.preferredExit)).toEqual([...initial, null])
          expect(logs).toHaveLength(0)
          await client.execute('DROP TRIGGER fail_map')
        }
        await load().syncClientPrefsFile()
        const actual = (await db.select().from(schema.clients).orderBy(schema.clients.id)).map(row => row.preferredExit)
        console.log(`${scenario.name}: ${JSON.stringify([...initial, null])} → ${JSON.stringify(actual)}`)
        expect(actual).toEqual([...scenario.expected, null])
        const prefs = JSON.parse(await readFile(cfg.clientPrefsFilePath, 'utf8'))
        for (const [i, tag] of scenario.expected.entries()) {
          if (tag) expect(prefs[tag]).toEqual([`10.66.66.${i + 2}`, `10.67.67.${i + 2}`, `10.68.68.${i + 2}`])
        }
        expect(Object.keys(prefs).sort()).toEqual(scenario.expected.filter(Boolean).sort())
        const changed = scenario.expected.filter((tag, i) => tag !== initial[i]).length
        expect(logs).toHaveLength(changed)
        // Рестарт с прежним bootstrap-env не переносит уже перенесённых клиентов повторно.
        await load().syncClientPrefsFile()
        expect((await db.select().from(schema.clients).orderBy(schema.clients.id)).map(row => row.preferredExit)).toEqual(actual)
        expect(logs).toHaveLength(changed)
      }
      finally {
        client.close()
        await rm(directory, { recursive: true, force: true })
      }
    })
  }
}
