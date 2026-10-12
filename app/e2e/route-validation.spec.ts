import { expect, loginApi, test } from './support/fixtures'

test('маршруты отклоняют IPv4 с неверными октетами и маской', async ({ page }) => {
  await loginApi(page)
  for (const value of ['999.1.2.3', '256.0.0.1/24', '192.0.2.1/33', '192.000.2.1']) {
    const response = await page.request.post('/api/routes', { data: { value, outbound: 'direct-ru' } })
    try {
      expect(response.status(), value).toBe(400)
      expect(await response.json()).toMatchObject({ statusMessage: 'invalid_value' })
    }
    finally {
      if (response.ok()) await page.request.delete(`/api/routes/${(await response.json()).id}`)
    }
  }
  for (const value of ['192.0.2.1', '192.0.2.0/24', '0.0.0.0/0', '255.255.255.255/32']) {
    const response = await page.request.post('/api/routes', { data: { value, outbound: 'direct-ru' } })
    expect(response.status(), value).toBe(200)
    const route = await response.json()
    try {
      expect(route.type).toBe('ip_cidr')
    }
    finally {
      await page.request.delete(`/api/routes/${route.id}`)
    }
  }
})
