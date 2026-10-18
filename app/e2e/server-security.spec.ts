import { readdirSync } from 'node:fs'
import { expect, loginApi, test, uniq } from './support/fixtures'

test('CSRF: чужой Origin не может создать клиента через HTML-форму', async ({ page, guard, baseURL }) => {
  await loginApi(page)
  await page.context().setExtraHTTPHeaders({})
  const name = uniq('csrf')
  // Другой порт того же сайта: SameSite=Lax разрешает отправить cookie,
  // но источник формы не является origin панели. Origin ставит сам браузер.
  const attacker = new URL(baseURL!)
  attacker.port = String(Number(attacker.port || 80) + 1)
  await page.route(`${attacker.origin}/**`, route => route.fulfill({
    contentType: 'text/html',
    body: `<form method="post" action="${baseURL}/api/clients"><input name="name" value="${name}"><button>Create</button></form>`,
  }))
  guard.allow(403, /\/api\/clients$/, 'POST')
  await page.goto(attacker.origin)
  const pending = page.waitForResponse(r => new URL(r.url()).pathname === '/api/clients')
  await page.getByRole('button', { name: 'Create' }).click()
  const response = await pending
  expect(response.request().headers().origin).toBe(attacker.origin)
  expect(response.status()).toBe(403)
  const clients = await (await page.request.get(`${baseURL}/api/clients`)).json()
  expect(clients.some((c: { name: string }) => c.name === name)).toBe(false)
})

test('CSRF: cookie без Origin и Referer не разрешает изменение', async ({ page, playwright, baseURL }) => {
  await loginApi(page)
  const api = await playwright.request.newContext({
    baseURL,
    extraHTTPHeaders: {},
    storageState: await page.context().storageState(),
  })
  try {
    const response = await api.post('/api/clients', { data: { name: uniq('no-origin') } })
    expect(response.status()).toBe(403)
    const allowed = await api.post('/api/clients', {
      headers: { referer: `${baseURL}/` }, data: { name: uniq('referer') },
    })
    expect(allowed.status()).toBe(200)
  }
  finally {
    await api.dispose()
  }
})

test('CSRF: изменяющие запросы требуют источник панели', async ({ page }) => {
  await loginApi(page)
  for (const origin of ['https://attacker.example', 'null']) {
    for (const [method, url] of [
      ['POST', '/api/clients/999999/unlink'],
      ['PATCH', '/api/clients/999999'],
      ['DELETE', '/api/clients/999999'],
      ['PUT', '/api/admin/telegram'],
    ]) {
      const response = await page.request.fetch(url!, { method, headers: { origin }, data: {} })
      expect(response.status(), `${method} ${url}: ${await response.text()}`).toBe(403)
    }
  }
})

test('параллельное создание устройств соблюдает лимит клиента', async ({ page }) => {
  await loginApi(page)
  const created = await page.request.post('/api/clients', { data: { name: uniq('limit-race'), deviceLimit: 1 } })
  expect(created.status()).toBe(200)
  const client = await created.json()
  const responses = await Promise.all(Array.from({ length: 12 }, (_, i) =>
    page.request.post(`/api/clients/${client.id}/devices`, { data: { name: `device-${i}` } })))
  const statuses = responses.map(r => r.status())
  const detail = await (await page.request.get(`/api/clients/${client.id}`)).json()
  console.log(`device admission: statuses=${statuses.join(',')} count=${detail.devices.length}`)
  expect(statuses.filter(s => s === 200)).toHaveLength(1)
  expect(statuses.filter(s => s === 409)).toHaveLength(11)
  expect(detail.devices).toHaveLength(1)
})

test('все маршруты панели требуют сессию до чтения тела и обращения к устройствам', async ({ request }) => {
  const files = readdirSync('server/api', { recursive: true }) as string[]
  let checked = 0
  for (const file of files) {
    if (/^(bot\/|auth\/|sso\.|version\.|ops\/bot-snapshot\.)/.test(file)) continue
    const match = /^(.*)\.(get|post|put|patch|delete)\.ts$/.exec(file)
    if (!match) continue
    const path = `/api/${match[1]!.replace(/\/index$/, '').replace(/\[id\]|\[deviceId\]/g, '999999').replace('[tag]', 'nl')}`
    const response = await request.fetch(path, { method: match[2]!.toUpperCase() })
    // Фоновый опрос уведомлений переживает выход без 401, как get-session,
    // но без сессии отдаёт только пустой список.
    if (file === 'admin/bot-deliveries.get.ts') {
      expect(response.status(), file).toBe(200)
      expect(await response.json(), file).toEqual([])
      continue
    }
    expect(response.status(), `${file}: ${await response.text()}`).toBe(401)
    checked++
  }
  expect(checked).toBeGreaterThan(30)
  console.log(`anonymous panel routes: ${checked} returned 401`)
})

test('чужой clientId не открывает конфиги и операции устройства', async ({ page }) => {
  await loginApi(page)
  const create = async () => {
    const r = await page.request.post('/api/clients', { data: { name: uniq('ownership') } })
    expect(r.status()).toBe(200)
    return r.json()
  }
  const owner = await create()
  const other = await create()
  const added = await page.request.post(`/api/clients/${owner.id}/devices`, { data: { name: 'phone' } })
  expect(added.status()).toBe(200)
  const device = await added.json()
  const path = `/api/clients/${other.id}/devices/${device.id}`
  for (const suffix of ['wg-config', 'wg-qrcode.svg', 'ovpn-config', 'ikev2-credentials']) {
    expect((await page.request.get(`${path}/${suffix}`)).status(), suffix).toBe(404)
  }
  for (const suffix of ['reissue', 'send-wg-to-tg', 'send-ovpn-to-tg', 'send-ikev2-to-tg']) {
    expect((await page.request.post(`${path}/${suffix}`)).status(), suffix).toBe(404)
  }
  expect((await page.request.delete(path)).status()).toBe(404)
  expect((await page.request.get(`/api/clients/${owner.id}/devices/${device.id}/wg-config`)).status()).toBe(200)
})
