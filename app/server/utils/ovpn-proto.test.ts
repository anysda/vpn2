import { beforeEach, describe, expect, it, vi } from 'vitest'
import { buildOvpnConfig } from './openvpn'
import { control } from './control'

vi.mock('../database/client', async () => ({
  ...(await import('../test/stubs')).databaseModule,
  claimUnique: (await vi.importActual<typeof import('../database/client')>('../database/client')).claimUnique,
  isUniqueViolation: () => false,
}))
vi.mock('./control', () => ({ control: vi.fn(), controlApply: vi.fn() }))

describe('ovpn-proto', () => {
  beforeEach(() => {
    vi.mocked(control).mockReset()
    vi.mocked(control).mockResolvedValue({ ca: 'fixture', tlsCrypt: 'fixture' })
  })

  for (const proto of ['udp', 'udp4', 'udp6', 'tcp', 'tcp4', 'tcp6', 'tcp-server', 'tcp4-server', 'tcp6-server']) {
    for (const host of ['192.0.2.10', '2001:db8::10', 'vpn.example.org']) {
      it(`OpenVPN ${proto}: клиентский профиль для ${host}`, async () => {
        const profile = await buildOvpnConfig({
          proto, serverHost: host, serverPort: 1194, dns: [], clientCert: 'cert', clientKey: 'key',
        })
        expect(profile).toContain(`\nproto ${proto.startsWith('udp') ? 'udp' : 'tcp-client'}\n`)
        expect(profile).toContain(`\nremote ${host} 1194\n`)
        expect(profile.includes('\nfast-io\n')).toBe(proto.startsWith('udp'))
      })
    }
  }

  it('OpenVPN: неподдерживаемый серверный proto не попадает в профиль', async () => {
    const message = await buildOvpnConfig({ proto: 'tcp-client' } as never)
      .then(() => '', (error: Error) => error.message)
    expect(message).toContain('unsupported OpenVPN protocol')
  })
})
