import { readFileSync } from 'node:fs'
import path from 'node:path'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'
import { expect, test } from '@playwright/test'

const root = process.env.VPN2_TEST_ROOT || path.resolve('..')
const source = readFileSync(path.join(root, 'app/server/utils/openvpn.ts'), 'utf8')
const render = source.slice(source.indexOf('export async function buildOvpnConfig'), source.indexOf('\n/**', source.indexOf('export async function buildOvpnConfig')))
const code = ts.transpile(render.replace('export ', ''), { target: ts.ScriptTarget.ES2022 })

for (const proto of ['udp', 'udp4', 'udp6', 'tcp', 'tcp4', 'tcp6', 'tcp-server', 'tcp4-server', 'tcp6-server']) {
  for (const host of ['192.0.2.10', '2001:db8::10', 'vpn.example.org']) {
    test(`OpenVPN ${proto}: клиентский профиль для ${host}`, async () => {
      const profile = await runInNewContext(`${code}\nbuildOvpnConfig(params)`, {
        params: { proto, serverHost: host, serverPort: 1194, dns: [], clientCert: 'cert', clientKey: 'key' },
        fs: { readFile: async () => 'fixture' }, CA_CERT: '', TLS_CRYPT: '',
        control: async () => ({ ca: 'fixture', tlsCrypt: 'fixture' }),
        ovpnLocalRoutes: () => [],
      })
      expect(profile).toContain(`\nproto ${proto.startsWith('udp') ? 'udp' : 'tcp-client'}\n`)
      expect(profile).toContain(`\nremote ${host} 1194\n`)
      expect(profile.includes('\nfast-io\n')).toBe(proto.startsWith('udp'))
    })
  }
}

test('OpenVPN: неподдерживаемый серверный proto не попадает в профиль', async () => {
  const message = await runInNewContext(`${code}\nbuildOvpnConfig(params)`, {
    params: { proto: 'tcp-client' }, fs: { readFile: async () => '' },
    CA_CERT: '', TLS_CRYPT: '', ovpnLocalRoutes: () => [],
  }).then(() => '', (error: Error) => error.message)
  expect(message).toContain('unsupported OpenVPN protocol')
})
