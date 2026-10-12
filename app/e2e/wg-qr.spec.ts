import { BitMatrix, QRCodeReader } from '@zxing/library'
import { ADMIN_PASSWORD, expect, hydrated, loginApi, test, uniq } from './support/fixtures'

class ModuleReader extends QRCodeReader {
  decodeModules(matrix: BitMatrix) {
    return this.getDecoder().decodeBitMatrix(matrix)
  }
}

test('WG QR декодируется в выданный конфиг без зеркального восстановления', async ({ page }) => {
  await loginApi(page)
  const clientResponse = await page.request.post('/api/clients', { data: { name: uniq('qr') } })
  expect(clientResponse.ok()).toBeTruthy()
  const client = await clientResponse.json()
  const deviceResponse = await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'phone' } })
  expect(deviceResponse.ok()).toBeTruthy()
  const device = await deviceResponse.json()
  const url = `/api/clients/${client.id}/devices/${device.id}`
  const conf = await page.request.get(`${url}/wg-config`)
  const qr = await page.request.get(`${url}/wg-qrcode.svg`)
  expect(qr.ok()).toBeTruthy()
  const svg = await qr.text()
  const size = Number(/viewBox="0 0 (\d+) /.exec(svg)![1])
  const runs = [...svg.matchAll(/M(\d+) (\d+)h(\d+)v1h-\d+z/g)]
  expect(runs.length).toBeGreaterThan(0)
  const pad = Math.min(...runs.map(r => Number(r[1])))
  const matrix = new BitMatrix(size - 2 * pad)
  for (const [, x, y, length] of runs) {
    matrix.setRegion(Number(x) - pad, Number(y) - pad, Number(length), 1)
  }
  const decoded = new ModuleReader().decodeModules(matrix)
  expect(decoded.getText()).toBe(await conf.text())
  // ZXing восстанавливает зеркальные QR, но сканеры без этой эвристики их отвергают.
  expect.soft(decoded.getOther()?.isMirrored() ?? false).toBe(false)
  expect.soft(pad, 'тихая зона QR — минимум четыре модуля').toBeGreaterThanOrEqual(4)
})

test('QR настройки 2FA декодируется без зеркального восстановления', async ({ page }) => {
  await loginApi(page)
  await page.goto('/me')
  await hydrated(page)
  await page.getByPlaceholder('Текущий пароль', { exact: true }).fill(ADMIN_PASSWORD)
  const response = page.waitForResponse(r => r.url().endsWith('/api/auth/two-factor/enable'))
  await page.getByRole('button', { name: 'Включить 2FA', exact: true }).click()
  const { totpURI } = await (await response).json()
  const svg = page.locator('div.bg-white > svg')
  await expect(svg).toBeVisible()
  const { width, modules } = await svg.evaluate(el => ({
    width: Number(el.getAttribute('width')),
    modules: [...el.querySelectorAll('rect')].slice(1).map(r => ({
      x: Number(r.getAttribute('x')),
      y: Number(r.getAttribute('y')),
      width: Number(r.getAttribute('width')),
    })),
  }))
  const unit = modules[0]!.width
  const pad = Math.round(Math.min(...modules.map(m => m.x)) / unit)
  const matrix = new BitMatrix(Math.round(width / unit) - 2 * pad)
  for (const m of modules) matrix.set(Math.round(m.x / unit) - pad, Math.round(m.y / unit) - pad)
  const decoded = new ModuleReader().decodeModules(matrix)
  expect(decoded.getText()).toBe(totpURI)
  expect.soft(decoded.getOther()?.isMirrored() ?? false).toBe(false)
  expect.soft(pad).toBeGreaterThanOrEqual(4)
  await page.getByRole('button', { name: 'Отмена', exact: true }).click()
})
