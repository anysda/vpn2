import os from 'node:os'
import { mock } from 'node:test'
import { expect, test as baseTest } from '@playwright/test'
import QRCode from 'qrcode-svg'
import { buildWgClientConfig } from '../server/utils/wireguard'
import { Resolver } from 'node:dns/promises'
import { wgEndpointHasAAAA } from '../server/utils/wg-endpoint-dns'
import { downloadText, hydrated, loginApi, PUBLIC_HOST, uniq, test } from './support/fixtures'
import { configDialog, deviceCard, openClient } from './support/clients'

test.describe('wg-endpoint', () => {
  const test = baseTest

  const params = {
    clientPrivateKey: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
    clientPresharedKey: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
    serverPublicKey: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
    clientIp: '10.66.66.2',
    serverPort: 51900,
  }
  const originalIp = process.env.NUXT_WG_ENDPOINT_IP

  test.beforeEach(() => {
    delete process.env.NUXT_WG_ENDPOINT_IP
    mock.method(os, 'networkInterfaces', () => ({}))
  })
  test.afterEach(() => {
    mock.restoreAll()
    if (originalIp === undefined) delete process.env.NUXT_WG_ENDPOINT_IP
    else process.env.NUXT_WG_ENDPOINT_IP = originalIp
  })

  for (const splitLocal of [true, false]) {
    test(`WireGuard: IPv4 профиль побайтно прежний, splitLocal=${splitLocal}`, () => {
      const conf = buildWgClientConfig({ ...params, serverEndpoint: '203.0.113.7', splitLocal })
      expect(conf).toMatchSnapshot(`ipv4-${splitLocal}.conf`)
    })

    test(`WireGuard: IPv6 заменяется IPv4 без увеличения профиля и QR, splitLocal=${splitLocal}`, () => {
      process.env.NUXT_WG_ENDPOINT_IP = '203.0.113.7'
      const ipv4 = buildWgClientConfig({ ...params, serverEndpoint: '203.0.113.7', splitLocal })
      for (const host of ['2001:db8::1', '::1', '[2001:db8::1]', '::ffff:203.0.113.7']) {
        const conf = buildWgClientConfig({ ...params, serverEndpoint: host, splitLocal })
        expect(conf).toBe(ipv4)
        expect(Buffer.byteLength(conf)).toBeLessThanOrEqual(2953)
        expect(new QRCode({ content: conf, ecl: 'L' }).svg())
          .toBe(new QRCode({ content: ipv4, ecl: 'L' }).svg())
      }
    })
  }

  test('WireGuard: IPv6 из автоопределения заменяется публичным IPv4 интерфейса сервера', () => {
    process.env.NUXT_WG_ENDPOINT_IP = '2001:db8::1'
    mock.method(os, 'networkInterfaces', () => ({
      eth0: [{ address: '203.0.113.7', family: 'IPv4', internal: false }],
    }))
    expect(buildWgClientConfig({ ...params, serverEndpoint: '2001:db8::1' }))
      .toBe(buildWgClientConfig({ ...params, serverEndpoint: '203.0.113.7' }))
  })

  test('WireGuard: без публичного IPv4 профиль не выдаётся', () => {
    for (const fallback of ['', '2001:db8::1', 'vpn.example.test']) {
      process.env.NUXT_WG_ENDPOINT_IP = fallback
      mock.method(os, 'networkInterfaces', () => ({
        lo: [{ address: '127.0.0.1', family: 'IPv4', internal: true }],
        wg0: [{ address: '10.66.66.1', family: 'IPv4', internal: false }],
        eth0: [{ address: '172.20.0.2', family: 'IPv4', internal: false }],
      }))
      for (const serverEndpoint of ['2001:db8::1', '[2001:db8::1]']) {
        expect(() => buildWgClientConfig({ ...params, serverEndpoint })).toThrow(/IPv4.*сервера/)
      }
    }
  })

  test('WireGuard: доменный endpoint сохраняется в профиле', () => {
    const conf = buildWgClientConfig({ ...params, serverEndpoint: 'vpn.example.test' })
    expect(conf).toContain('Endpoint = vpn.example.test:51900\n')
  })

  test('WireGuard: неоднозначный IPv4 требует явной настройки', () => {
    mock.method(os, 'networkInterfaces', () => ({
      eth0: [{ address: '203.0.113.7', family: 'IPv4', internal: false }],
      eth1: [{ address: '198.51.100.7', family: 'IPv4', internal: false }],
    }))
    expect(() => buildWgClientConfig({ ...params, serverEndpoint: '2001:db8::1' }))
      .toThrow(/IPv4.*сервера/)
  })
})

test.describe('wg-endpoint-dns', () => {
  const test = baseTest

  test.afterEach(() => mock.restoreAll())

  test('WG DNS: IP-литералы не вызывают DNS', async () => {
    const dns = mock.method(Resolver.prototype, 'resolve6', async () => ['2001:db8::1'])
    for (const host of ['', '203.0.113.7', '2001:db8::1', '[2001:db8::1]']) {
      expect(await wgEndpointHasAAAA(host)).toBe(false)
    }
    expect(dns.mock.callCount()).toBe(0)
  })

  test('WG DNS: кэш AAAA истекает через минуту, смена домена проверяется сразу', async () => {
    let now = 0
    mock.method(Date, 'now', () => now)
    const dns = mock.method(Resolver.prototype, 'resolve6', async () => ['2001:db8::1'])
    expect(await wgEndpointHasAAAA('cache.example.test')).toBe(true)
    now = 59_999
    expect(await wgEndpointHasAAAA('CACHE.example.test.')).toBe(true)
    expect(dns.mock.callCount()).toBe(1)
    now = 60_000
    expect(await wgEndpointHasAAAA('cache.example.test')).toBe(true)
    expect(dns.mock.callCount()).toBe(2)
    expect(await wgEndpointHasAAAA('changed.example.test')).toBe(true)
    expect(dns.mock.callCount()).toBe(3)
  })

  test('WG DNS: ошибка резолвера не выходит наружу и кэшируется', async () => {
    const dns = mock.method(Resolver.prototype, 'resolve6', async () => { throw new Error('DNS unavailable') })
    expect(await wgEndpointHasAAAA('error.example.test')).toBe(false)
    expect(await wgEndpointHasAAAA('error.example.test')).toBe(false)
    expect(dns.mock.callCount()).toBe(1)
  })

  test('WG DNS: зависший резолвер отменяется по дедлайну', async () => {
    mock.method(Resolver.prototype, 'resolve6', () => new Promise(() => {}))
    const cancel = mock.method(Resolver.prototype, 'cancel', () => {})
    const start = performance.now()
    expect(await wgEndpointHasAAAA('timeout.example.test')).toBe(false)
    expect(performance.now() - start).toBeLessThan(1000)
    expect(cancel.mock.callCount()).toBe(1)
  })
})

test.describe('wg-endpoint-warning', () => {
  const warningText = 'Уберите у домена IPv6-запись (AAAA), иначе у клиентов на Linux VPN может не заработать.'

  for (const dns of ['aaaa', 'a-only', 'unavailable']) {
    test(`WG: предупреждение в диалоге при DNS ${dns}, профиль доступен`, async ({ page }) => {
      await loginApi(page)
      const name = uniq('wg-dns')
      const client = await (await page.request.post('/api/clients', { data: { name } })).json()
      await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'phone' } })
      await page.route('**/api/wg-endpoint-warning', route => route.fulfill({
        json: { hasAAAA: dns === 'aaaa' },
      }))
      await page.goto('/clients')
      await hydrated(page)
      await openClient(page, name)
      const checked = page.waitForResponse('**/api/wg-endpoint-warning')
      await deviceCard(page, 'phone').wg.click()
      await checked
      const dialog = configDialog(page, 'phone', 'WireGuard')
      const warning = dialog.getByText(warningText, { exact: true })
      if (dns === 'aaaa') await expect(warning).toBeVisible()
      else await expect(warning).toHaveCount(0)
      await expect(dialog.getByRole('button', { name: 'Копировать', exact: true })).toBeEnabled()
      const downloaded = page.waitForEvent('download')
      await dialog.getByRole('button', { name: 'Скачать', exact: true }).click()
      expect(await downloadText(await downloaded)).toContain(`Endpoint = ${PUBLIC_HOST}:51820\n`)
    })
  }

  test('WG: ожидание DNS не задерживает профиль, повторное открытие очищает предупреждение', async ({ page }) => {
    await loginApi(page)
    const name = uniq('wg-dns-delay')
    const client = await (await page.request.post('/api/clients', { data: { name } })).json()
    await page.request.post(`/api/clients/${client.id}/devices`, { data: { name: 'phone' } })
    let pending = false
    await page.route('**/api/wg-endpoint-warning', async (route) => {
      if (pending) return // запрос висит до клиентского таймаута
      await route.fulfill({ json: { hasAAAA: true } })
    })
    await page.goto('/clients')
    await hydrated(page)
    await openClient(page, name)
    await deviceCard(page, 'phone').wg.click()
    const dialog = configDialog(page, 'phone', 'WireGuard')
    await expect(dialog.getByText(warningText)).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
    pending = true
    await deviceCard(page, 'phone').wg.click()
    await expect(dialog.getByText(warningText)).toHaveCount(0)
    await expect(dialog.getByRole('button', { name: 'Скачать', exact: true })).toBeEnabled()
  })
})
