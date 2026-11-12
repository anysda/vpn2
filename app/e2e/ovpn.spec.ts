import { X509Certificate } from 'node:crypto'
import { expect, loginApi, uniq, test } from './support/fixtures'
import { mkdtempSync, rmSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { migrate } from 'drizzle-orm/libsql/migrator'
import type * as Schema from '../server/database/schema'
import type * as Database from '../server/database/client'
import type * as Openvpn from '../server/utils/openvpn'
import { test as baseTest } from '@playwright/test'

test.describe('ovpn-ca', () => {
  test('параллельная выдача OpenVPN сохраняет уникальные сертификаты', async ({ page }) => {
    await loginApi(page)
    const created = await page.request.post('/api/clients', { data: { name: uniq('ca'), deviceLimit: null } })
    expect(created.status()).toBe(200)
    const client = await created.json()
    try {
      const responses = await Promise.all(Array.from({ length: 16 }, (_, i) =>
        page.request.post(`/api/clients/${client.id}/devices`, { data: { name: `device-${i}` } }),
      ))
      for (const r of responses) expect(r.status()).toBe(200)
      const detail = await (await page.request.get(`/api/clients/${client.id}`)).json()
      expect(detail.devices.filter((d: { hasOvpn: boolean }) => d.hasOvpn)).toHaveLength(16)
      const serials = await Promise.all(detail.devices.map(async (d: { id: number }) => {
        const r = await page.request.get(`/api/clients/${client.id}/devices/${d.id}/ovpn-config`)
        expect(r.status()).toBe(200)
        const pem = /<cert>\s*([\s\S]+?)\s*<\/cert>/.exec(await r.text())![1]!
        return new X509Certificate(pem).serialNumber
      }))
      expect(new Set(serials).size, JSON.stringify(serials)).toBe(16)
    }
    finally {
      await page.request.delete(`/api/clients/${client.id}`)
    }
  })
})

test.describe('ovpn-reconcile', () => {
  const test = baseTest

  test('OpenVPN повторяет разрыв удалённого устройства после сбоя management', async () => {
    const root = process.env.VPN2_TEST_ROOT || path.resolve('..')
    const directory = mkdtempSync(path.join(tmpdir(), 'ovpn-reconcile-'))
    const killed: number[] = []
    try {
      const app = createRequire(import.meta.url)('./support/access-harness.cjs')(root, path.join(directory, 'db.sqlite'), {}, { ovpnEnabled: true })
      const schema: typeof Schema = app.load('app/server/database/schema.ts')
      const db = (app.load('app/server/database/client.ts') as typeof Database).useDb()
      await migrate(db, { migrationsFolder: path.join(root, 'app/server/database/migrations') })
      const [client] = await db.insert(schema.clients).values({ name: 'reconcile', password: 'fixture' }).returning()
      await db.insert(schema.devices).values({ id: 43, clientId: client!.id, name: 'phone', ovpnCert: 'cert', ovpnIp: '10.67.67.3' })
      app.overrides.set('app/server/utils/control.ts', {
        ControlUnavailable: class extends Error {},
        controlApply: async () => ({}),
        control: async (method: string, params: { device_id?: number }) => {
          if (method === 'profile.read') return { ready: true }
          // Удаление dev_42 уже закоммичено, но разрыв при удалении не дошёл до сервера.
          if (method === 'state.read') return { sessions: { dev_42: '10.67.67.2', dev_43: '10.67.67.3', external: '10.67.67.240' } }
          if (method === 'openvpn.disconnect') killed.push(params.device_id!)
          return { killed: true }
        },
      })
      await (app.load('app/server/utils/openvpn.ts') as typeof Openvpn).syncOpenvpnConfig()
    }
    finally { rmSync(directory, { recursive: true, force: true }) }
    expect(killed).toEqual([42])
  })
})
