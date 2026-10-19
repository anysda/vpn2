import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { expect, test } from '@playwright/test'
import type * as Schema from '../server/database/schema'
import type * as Database from '../server/database/client'

const require = createRequire(import.meta.url)
const harness = require('./support/access-harness.cjs')
const root = process.env.VPN2_TEST_ROOT || path.resolve('..')
const routes = [
  'clients/[id].patch', 'clients/[id].delete', 'clients/[id]/reissue.post',
  'clients/[id]/devices/[deviceId].delete', 'clients/[id]/devices/[deviceId]/reissue.post',
  'bot/admin/clients/[id].patch', 'bot/admin/clients/[id].delete',
  'bot/devices/[deviceId].delete', 'bot/devices/[deviceId]/reissue.post',
  'access-operations/[id]/retry.post', 'bot/admin/access-operations/[id]/retry.post',
]

for (const route of routes) {
  test(`${route}: принятие операции не сообщает окончательный успех`, async () => {
    const directory = mkdtempSync(path.join(tmpdir(), 'access-route-'))
    try {
      const app = harness(root, path.join(directory, 'db.sqlite'), {})
      const schema: typeof Schema = app.load('app/server/database/schema.ts')
      const db = (app.load('app/server/database/client.ts') as typeof Database).useDb()
      await migrate(db, { migrationsFolder: path.join(root, 'app/server/database/migrations') })
      const [client] = await db.insert(schema.clients).values({ name: 'route', password: 'fixture', tgChatId: 123 }).returning()
      const [device] = await db.insert(schema.devices).values({ clientId: client!.id, name: 'phone' }).returning()
      const operation = { id: 'accepted-operation', status: 'pending', version: 1 }
      const result = { ...client, operation }
      let notifications = 0
      app.overrides.set('app/server/utils/auth.ts', { requireAuth: async () => ({ id: 1 }) })
      app.overrides.set('app/server/utils/events.ts', { actorOf: () => 'panel', BOT_ACTOR: 'bot', recordEvent: async () => {}, joinDetail: () => '' })
      app.overrides.set('app/server/utils/validate.ts', { readBodyAs: async () => ({ chatId: 123, frozenManual: true }) })
      app.patch('app/server/utils/bot-events.ts', { notifyClient: async () => { notifications++ } })
      app.overrides.set('app/server/utils/client-ops.ts', {
        updateClient: async () => result, deleteClient: async () => ({ operation }), deleteDevice: async () => ({ ...device, operation }),
      })
      app.overrides.set('app/server/utils/access-operations.ts', { accessRequestKey: () => 'request-key', enqueueAccess: async () => ({ client, operation }), retryAccess: async () => operation })
      app.overrides.set('app/server/utils/bot-api.ts', {
        requireBotAuth: () => {}, clientByChat: async () => client, botClientView: async () => ({ ...client, devices: [device] }),
      })
      for (const protocol of ['wireguard', 'openvpn', 'ikev2']) {
        app.overrides.set(`app/server/utils/${protocol}.ts`, {
          reissueDeviceWg: async () => {}, reissueDeviceOvpn: async () => {}, reissueDeviceIkev2: async () => {},
          syncWireguardConfig: async () => {}, syncOpenvpnConfig: async () => {}, syncIkev2: async () => {},
        })
      }
      const event = { params: { id: String(client!.id), deviceId: String(device!.id) }, query: { chatId: 123 }, status: 200 }
      const handler = app.load(`app/server/api/${route}.ts`).default
      const response = await handler(event)
      expect(event.status).toBe(202)
      expect(response.operation).toEqual(operation)
      expect(response.ok).toBeUndefined()
      expect(JSON.stringify(response)).not.toMatch(/Доступ отключён|Доступ включён|Ключи перевыпущены/)
      expect(notifications).toBe(0)
      console.log(`${route}: 202, operation.id, pending, notifications=0`)
    }
    finally { rmSync(directory, { recursive: true, force: true }) }
  })
}
