import { expect, loginApi, test, uniq } from './support/fixtures'

test('имена файлов конфигов помещаются в имя интерфейса WireGuard и различают устройства', async ({ page }) => {
  await loginApi(page)
  const response = await page.request.post('/api/clients', { data: { name: uniq('long-client-name') } })
  expect(response.ok()).toBeTruthy()
  const client = await response.json()
  for (const name of ['Щ'.repeat(63) + '1', 'Щ'.repeat(63) + '2']) {
    const r = await page.request.post(`/api/clients/${client.id}/devices`, { data: { name } })
    expect(r.ok()).toBeTruthy()
  }
  const detail = await (await page.request.get(`/api/clients/${client.id}`)).json()
  const names = detail.devices.map((d: { configName: string }) => d.configName)
  expect(new Set(names).size).toBe(2)
  for (const name of names) expect(name).toMatch(/^[a-zA-Z0-9_=+.-]{1,15}$/)
})
