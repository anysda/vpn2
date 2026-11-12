import { readFileSync } from 'node:fs'
import os from 'node:os'
import QRCode from 'qrcode-svg'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { buildWgClientConfig } from './wireguard'

vi.mock('../database/client', async () => ({
  ...(await import('../test/stubs')).databaseModule,
  claimUnique: (await vi.importActual<typeof import('../database/client')>('../database/client')).claimUnique,
}))
vi.mock('./control', () => ({ control: vi.fn(), controlApply: vi.fn() }))

type Interfaces = ReturnType<typeof os.networkInterfaces>

describe('wg-endpoint', () => {
  const params = {
    clientPrivateKey: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
    clientPresharedKey: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
    serverPublicKey: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
    clientIp: '10.66.66.2',
    serverPort: 51900,
  }
  const originalIp = process.env.NUXT_WG_ENDPOINT_IP
  const interfaces = (value: object) => vi.spyOn(os, 'networkInterfaces').mockImplementation(() => value as Interfaces)
  const fixture = (name: string) => readFileSync(new URL(`../test/fixtures/${name}`, import.meta.url), 'utf8')

  beforeEach(() => {
    delete process.env.NUXT_WG_ENDPOINT_IP
    interfaces({})
  })
  afterEach(() => {
    vi.restoreAllMocks()
    if (originalIp === undefined) delete process.env.NUXT_WG_ENDPOINT_IP
    else process.env.NUXT_WG_ENDPOINT_IP = originalIp
  })

  for (const splitLocal of [true, false]) {
    it(`WireGuard: IPv4 профиль побайтно прежний, splitLocal=${splitLocal}`, () => {
      const conf = buildWgClientConfig({ ...params, serverEndpoint: '203.0.113.7', splitLocal })
      expect(conf).toBe(fixture(`wg-endpoint-ipv4-${splitLocal}.conf`))
    })

    it(`WireGuard: IPv6 заменяется IPv4 без увеличения профиля и QR, splitLocal=${splitLocal}`, () => {
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

  it('WireGuard: IPv6 из автоопределения заменяется публичным IPv4 интерфейса сервера', () => {
    process.env.NUXT_WG_ENDPOINT_IP = '2001:db8::1'
    interfaces({ eth0: [{ address: '203.0.113.7', family: 'IPv4', internal: false }] })
    expect(buildWgClientConfig({ ...params, serverEndpoint: '2001:db8::1' }))
      .toBe(buildWgClientConfig({ ...params, serverEndpoint: '203.0.113.7' }))
  })

  it('WireGuard: без публичного IPv4 профиль не выдаётся', () => {
    for (const fallback of ['', '2001:db8::1', 'vpn.example.test']) {
      process.env.NUXT_WG_ENDPOINT_IP = fallback
      interfaces({
        lo: [{ address: '127.0.0.1', family: 'IPv4', internal: true }],
        wg0: [{ address: '10.66.66.1', family: 'IPv4', internal: false }],
        eth0: [{ address: '172.20.0.2', family: 'IPv4', internal: false }],
      })
      for (const serverEndpoint of ['2001:db8::1', '[2001:db8::1]']) {
        expect(() => buildWgClientConfig({ ...params, serverEndpoint })).toThrow(/IPv4.*сервера/)
      }
    }
  })

  it('WireGuard: доменный endpoint сохраняется в профиле', () => {
    const conf = buildWgClientConfig({ ...params, serverEndpoint: 'vpn.example.test' })
    expect(conf).toContain('Endpoint = vpn.example.test:51900\n')
  })

  it('WireGuard: неоднозначный IPv4 требует явной настройки', () => {
    interfaces({
      eth0: [{ address: '203.0.113.7', family: 'IPv4', internal: false }],
      eth1: [{ address: '198.51.100.7', family: 'IPv4', internal: false }],
    })
    expect(() => buildWgClientConfig({ ...params, serverEndpoint: '2001:db8::1' }))
      .toThrow(/IPv4.*сервера/)
  })
})
