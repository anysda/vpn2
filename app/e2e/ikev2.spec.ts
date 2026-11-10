import { expect, loginApi, uniq, hydrated, test } from './support/fixtures'
import { openClient } from './support/clients'
import { container, inside } from './support/openvpn-ca'
import { test as baseTest } from '@playwright/test'
import { createClient } from '@libsql/client'
import { drizzle } from 'drizzle-orm/libsql'
import { migrate } from 'drizzle-orm/libsql/migrator'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import * as schema from '../server/database/schema'
import { nextPoolAddress } from '../server/utils/address-pools'
import { coalesceRuns } from '../server/utils/coalesce'
import { slugify } from '../server/utils/naming'
import { serverModule } from './support/server-module'

test.describe('ikev2-delivery', () => {
  test('отправка IKEv2 сообщает об ошибке бота так же, как отправка WG', async ({ page }) => {
    await loginApi(page)
    const created = await page.request.post('/api/clients', { data: { name: uniq('delivery') } })
    expect(created.status()).toBe(200)
    const client = await created.json()
    try {
      const added = await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'phone' } })
      expect(added.status()).toBe(200)
      const device = await added.json()
      const url = `/api/clients/${client.id}/devices/${device.id}`
      const wg = await page.request.post(`${url}/send-wg-to-tg`)
      test.skip(wg.ok(), 'бот доступен — ошибка доставки не воспроизводится')
      expect([502, 503]).toContain(wg.status())
      const ikev2 = await page.request.post(`${url}/send-ikev2-to-tg`)
      expect(ikev2.status()).toBe(wg.status())
      expect((await ikev2.json()).statusMessage).toBe((await wg.json()).statusMessage)
    }
    finally {
      await page.request.delete(`/api/clients/${client.id}`)
    }
  })
})

test.describe('ikev2-domain', () => {
  test('переход IKEv2 на домен сохраняет пароль и не требует перевыпуска старых профилей', async ({ page }) => {
    test.skip(!container, 'нужен одноразовый контейнер run-local.sh')
    await loginApi(page)
    const name = uniq('ike-domain')
    const client = await (await page.request.post('/api/clients', { data: { name } })).json()
    const device = await (await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'phone' } })).json()
    const url = `/api/clients/${client.id}/devices/${device.id}/ikev2-credentials`
    const saved = inside(`
    import fs from 'node:fs';
    const names = ['mode', 'server-host', 'domain'];
    console.log(JSON.stringify(Object.fromEntries(names.map(n => [n, fs.existsSync('/etc/anysda/ikev2-' + n) ? fs.readFileSync('/etc/anysda/ikev2-' + n, 'utf8') : null]))));
  `)
    try {
      const old = await (await page.request.get(url)).json()
      inside(`
      import fs from 'node:fs';
      fs.writeFileSync('/etc/anysda/ikev2-domain', 'vpn.example.test');
    `)
      const pending = await (await page.request.get(url)).json()
      expect(pending.certificatePending).toBe(true)
      expect(pending).not.toHaveProperty('profileNotice')
      expect(pending.server).toBe(old.server)
      inside(`
      import fs from 'node:fs';
      fs.writeFileSync('/etc/anysda/ikev2-server-host', 'vpn.example.test');
      fs.writeFileSync('/etc/anysda/ikev2-mode', 'letsencrypt');
    `)
      const current = await (await page.request.get(url)).json()
      expect(current).toMatchObject({
        server: 'vpn.example.test', remoteId: 'vpn.example.test', caCertPem: null,
        username: old.username, password: old.password, certificatePending: false,
      })
      await page.goto('/clients')
      await hydrated(page)
      await openClient(page, name)
      await page.getByRole('button', { name: 'IKEv2', exact: true }).click()
      await expect(page.getByText('Remote ID (iOS) / Server identity (Android)', { exact: true })).toBeVisible()
      await expect(page.getByText('нужно скачать заново')).toHaveCount(0)
    }
    finally {
      inside(`
      import fs from 'node:fs';
      for (const [n, value] of Object.entries(JSON.parse(process.argv[1]))) {
        const p = '/etc/anysda/ikev2-' + n;
        if (value === null) fs.rmSync(p, { force: true }); else fs.writeFileSync(p, value);
      }
    `, JSON.parse(saved))
      await page.request.delete(`/api/clients/${client.id}`)
    }
  })
})

test.describe('ikev2-repair', () => {
  const test = baseTest

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
          './coalesce': { coalesceRuns }, './client-status': { isClientActive: () => true, expiryForControl: () => null },
          './client-dns': { clientDns: () => ['192.0.2.53'] },
          './control': { controlApply: async (_method: string, params: unknown) => {
            config = execFileSync('python3', ['e2e/support/render-ikev2.py'], { input: JSON.stringify(params), encoding: 'utf8' })
          } },
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
        }, { useRuntimeConfig: () => ({ ikev2Enabled: true }), useLogger: () => ({ debug: () => {}, warn: () => {} }), process },
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
})
