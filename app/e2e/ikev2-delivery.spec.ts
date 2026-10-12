import { expect, loginApi, test, uniq } from './support/fixtures'

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
