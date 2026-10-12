import { expect, loginApi, test, uniq } from './support/fixtures'
import { container } from './support/openvpn-ca'
import { proxiesBarrier as proxies } from './support/proxies-barrier'

test('лимит проверяется при записи после параллельного создания устройства', async ({ page }) => {
  test.skip(!container, 'барьер sing-box доступен только в одноразовом e2e-контейнере')
  await loginApi(page)
  const created = await page.request.post('/api/clients', { data: { name: uniq('limit-race'), deviceLimit: 3 } })
  expect(created.status()).toBe(200)
  const client = await created.json()
  const url = `/api/clients/${client.id}`
  try {
    expect((await page.request.post(`${url}/devices`, { data: { name: 'first' } })).status()).toBe(200)
    proxies('POST')
    const updating = page.request.patch(url, { data: { deviceLimit: 1, preferredExit: 'nl' } })
    try {
      await expect.poll(() => proxies('GET').pending, { timeout: 2000, intervals: [50] }).toBe(1)
      const added = await page.request.post(`${url}/devices`, { data: { name: 'second' } })
      expect(added.status(), await added.text()).toBe(200)
    }
    finally {
      proxies('DELETE')
    }
    const result = await updating
    const after = await (await page.request.get(url)).json()
    console.log(`limit race: PATCH=${result.status()}, limit=${after.deviceLimit}, devices=${after.devices.length}`)
    expect(result.status()).toBe(409)
    expect(await result.json()).toMatchObject({ statusMessage: 'device_limit_below_count' })
    expect(after.deviceLimit).toBe(3)
    expect(after.preferredExit).toBeNull()
    expect(after.devices).toHaveLength(2)
  }
  finally {
    proxies('DELETE')
    await page.request.delete(url)
  }
})
