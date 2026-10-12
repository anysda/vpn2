import os from 'node:os'
import { mock } from 'node:test'
import { expect, test } from '@playwright/test'
import QRCode from 'qrcode-svg'
import { buildWgClientConfig } from '../server/utils/wireguard'

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

test('WireGuard: доменный endpoint сохранён до решения владельца', () => {
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
