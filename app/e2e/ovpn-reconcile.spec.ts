import { readFileSync } from 'node:fs'
import path from 'node:path'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'
import { expect, test } from '@playwright/test'

test('OpenVPN повторяет разрыв удалённого устройства после сбоя management', async () => {
  const root = process.env.VPN2_TEST_ROOT || path.resolve('..')
  const source = readFileSync(path.join(root, 'app/server/utils/openvpn.ts'), 'utf8')
  const sync = source.slice(source.indexOf('export async function syncOpenvpnConfig'))
  const code = ts.transpile(sync.replace('export ', ''), { target: ts.ScriptTarget.ES2022 })
  const killed: string[] = []
  const db = {
    select: () => db,
    from: () => db,
    innerJoin: async () => [{ id: 43, ovpnCert: 'cert', ovpnIp: '10.67.67.3', frozenManual: false }],
  }
  await runInNewContext(`${code}\nsyncOpenvpnConfig()`, {
    useRuntimeConfig: () => ({ ovpnEnabled: true }),
    caReady: async () => true,
    useDb: () => db,
    devicesTable: {}, clientsTable: {}, eq: () => true,
    ovpnCn: (id: number) => `dev_${id}`,
    isClientActive: () => true,
    writeCcd: async () => {},
    fs: { readdir: async () => [] }, CCD_DIR: '/unused',
    // Удаление dev_42 уже закоммичено, но kill при удалении не дошёл до сервера.
    connectedOvpnClients: async () => new Map([
      ['dev_42', '10.67.67.2'], ['dev_43', '10.67.67.3'], ['external', '10.67.67.240'],
    ]),
    killOvpnClientQuiet: async (cn: string) => { killed.push(cn) },
  })
  expect(killed).toEqual(['dev_42'])
})
