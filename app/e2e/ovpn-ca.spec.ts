import { X509Certificate } from 'node:crypto'
import { expect, loginApi, test, uniq } from './support/fixtures'

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
