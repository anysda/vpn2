import { expect, test } from '@playwright/test'
import { buildWgClientConfig } from '../server/utils/wireguard'

test('WireGuard: IPv6 endpoint отделён от порта квадратными скобками', () => {
  for (const host of ['2001:db8::1', '::1', '[2001:db8::1]', 'vpn.example.test', '203.0.113.7']) {
    const config = buildWgClientConfig({
      clientPrivateKey: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
      clientPresharedKey: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
      serverPublicKey: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
      clientIp: '10.66.66.2',
      serverEndpoint: host,
      serverPort: 51900,
      splitLocal: false,
    })
    const endpoint = /^Endpoint = (.+)$/m.exec(config)![1]!
    // Формат WireGuard — [IPv6]:port; Windows/Android отвергают IPv6
    // без скобок. Уже заключённый в скобки host не удваиваем.
    const parsed = new URL(`udp://${endpoint}`)
    expect(parsed.hostname).toBe(host.includes(':') && !host.startsWith('[') ? `[${host}]` : host)
    expect(parsed.port).toBe('51900')
  }
})
