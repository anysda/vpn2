import { expect, loginApi, test, uniq } from './support/fixtures'
import { container } from './support/openvpn-ca'
import { proxiesBarrier as proxies } from './support/proxies-barrier'

test('имя проверяется при переименовании после параллельного создания клиента', async ({ page }) => {
  test.skip(!container, 'барьер sing-box доступен только в одноразовом e2e-контейнере')
  await loginApi(page)
  const created = await page.request.post('/api/clients', { data: { name: uniq('rename-race') } })
  expect(created.status()).toBe(200)
  const client = await created.json()
  const url = `/api/clients/${client.id}`
  const name = uniq('claimed-name')
  try {
    proxies('POST', { clientName: name })
    const result = await page.request.patch(url, { data: { name, preferredExit: 'nl' } })
    expect(proxies('GET').insertedId, 'конкурентная запись выполнена после первой проверки имени').not.toBeNull()
    const all: Array<{ name: string }> = await (await page.request.get('/api/clients')).json()
    const matching = all.filter(client => client.name === name).length
    console.log(`rename race: PATCH=${result.status()}, clients with requested name=${matching}`)
    expect(result.status()).toBe(409)
    expect(matching).toBe(1)
    expect(await result.json()).toMatchObject({ statusMessage: 'client_exists' })
    const after = await (await page.request.get(url)).json()
    expect(after.name).toBe(client.name)
    expect(after.preferredExit).toBeNull()
  }
  finally {
    const otherId = proxies('DELETE').insertedId
    await page.request.delete(url)
    if (otherId) await page.request.delete(`/api/clients/${otherId}`)
  }
})
