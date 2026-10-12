import { expect, loginApi, test, uniq } from './support/fixtures'

test('пустое после trim имя клиента отклоняется при создании', async ({ page }) => {
  await loginApi(page)
  const response = await page.request.post('/api/clients', { data: { name: ' \t\n ' } })
  // В старой версии клиент уже создан: убираем его, хотя имя не имеет e2e-префикса.
  if (response.ok()) {
    const client = await response.json()
    await page.request.delete(`/api/clients/${client.id}`)
  }
  expect(response.status()).toBe(400)
})

test('пустое после trim имя не стирает имя существующего клиента', async ({ page }) => {
  await loginApi(page)
  const name = uniq('name-validation')
  const created = await page.request.post('/api/clients', { data: { name } })
  expect(created.status()).toBe(200)
  const client = await created.json()
  try {
    const response = await page.request.patch(`/api/clients/${client.id}`, { data: { name: '\u00a0\u2003' } })
    expect(response.status()).toBe(400)
    expect((await (await page.request.get(`/api/clients/${client.id}`)).json()).name).toBe(name)
  }
  finally {
    await page.request.delete(`/api/clients/${client.id}`)
  }
})

test('пустое после trim имя устройства не создаёт запись и ключи', async ({ page }) => {
  await loginApi(page)
  const created = await page.request.post('/api/clients', { data: { name: uniq('device-name') } })
  expect(created.status()).toBe(200)
  const client = await created.json()
  try {
    const response = await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: '   ' } })
    expect(response.status()).toBe(400)
    expect((await (await page.request.get(`/api/clients/${client.id}`)).json()).devices).toEqual([])
    const valid = await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: ' phone ' } })
    expect(valid.status()).toBe(200)
    expect((await valid.json()).name).toBe('phone')
  }
  finally {
    await page.request.delete(`/api/clients/${client.id}`)
  }
})
