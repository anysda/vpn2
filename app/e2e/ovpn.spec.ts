import { X509Certificate } from 'node:crypto'
import { expect, loginApi, uniq, test } from './support/fixtures'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'
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
    const source = readFileSync(path.join(root, 'app/server/utils/openvpn.ts'), 'utf8')
    const sync = source.slice(source.indexOf('export async function syncOpenvpnConfig'))
    const code = ts.transpile(sync.replace('export ', ''), { target: ts.ScriptTarget.ES2022 })
    const killed: string[] = []
    const db = {
      select: () => db,
      from: () => db,
      innerJoin: async () => [{ id: 43, ovpnCert: 'cert', ovpnIp: '10.67.67.3', frozenManual: false }],
    }
    await runInNewContext(`${code}\nsyncOpenvpnConfig()`, {
      withCaLock: (run: () => Promise<void>) => run(),
      useRuntimeConfig: () => ({ ovpnEnabled: true }),
      caReady: async () => true,
      useDb: () => db,
      devicesTable: {}, clientsTable: {}, eq: () => true,
      ovpnCn: (id: number) => `dev_${id}`,
      isClientActive: () => true,
      writeCcd: async () => {},
      fs: { readdir: async () => [] }, CCD_DIR: '/unused',
      // Удаление dev_42 уже закоммичено, но kill при удалении не дошёл до сервера.
      connectedOvpnClients: async () => new Map([
        ['dev_42', '10.67.67.2'], ['dev_43', '10.67.67.3'], ['external', '10.67.67.240'],
      ]),
      killOvpnClientQuiet: async (cn: string) => { killed.push(cn) },
    })
    expect(killed).toEqual(['dev_42'])
  })
})
