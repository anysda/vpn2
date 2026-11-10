import { readdirSync, readFileSync } from 'node:fs'
import { isDeepStrictEqual } from 'node:util'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { dbResults, installStubs } from '../test/stubs'
import { control, controlApply } from './control'
import { recordEvent } from './events'
import { loadServerKeys, syncWireguardConfig } from './wireguard'
import { caReady, ensureDeviceOvpn, killOvpnClient, revokeAccessCert } from './openvpn'
import { ikev2Mode, syncIkev2 } from './ikev2'
import { reportControlExpiry, syncControlExpiry } from './expiry'
import { toggleExit } from './exit-control'
import { syncRoutesFile } from './routes-sync'

// Примеры запросов и ответов control - общие со службой: те же запросы
// infra/tests/test_control.py пропускает через проверку contract.py.
// Поле version в *.apply дописывает controlApply, вызывающий код его не шлёт.
interface Example { method: string, params: Record<string, unknown>, result: unknown }
const serverDir = fileURLToPath(new URL('..', import.meta.url))
const examples: Example[] = JSON.parse(readFileSync(
  fileURLToPath(new URL('../../../infra/tests/fixtures/control/panel-calls.json', import.meta.url)), 'utf8'))

vi.mock('../database/client', async () => ({
  ...(await import('../test/stubs')).databaseModule,
  IP_CLAIM_ATTEMPTS: 50,
  isUniqueViolation: () => false,
}))
vi.mock('./control', () => ({ control: vi.fn(), controlApply: vi.fn() }))
vi.mock('./access-lock', () => ({ withAccessLock: (action: () => Promise<void>) => action() }))
vi.mock('./events', async importOriginal => ({ ...(await importOriginal<object>()), recordEvent: vi.fn(async () => true) }))

const sent: Example[] = []
/** Отвечает записанным примером; запроса, которого в примерах нет, панель слать не должна. */
function answer(apply: boolean) {
  return (async (method: string, params: Record<string, unknown>) => {
    const wire = apply ? { ...params, version: 1 } : params
    const example = examples.find(e => e.method === method && isDeepStrictEqual(e.params, wire))
    if (!example) throw new Error(`нет записанного примера: ${method} ${JSON.stringify(wire)}`)
    sent.push(example)
    return example.result
  }) as never
}
const sentMethods = () => sent.map(e => e.method === 'settings.apply' || e.method.endsWith('.read')
  ? `${e.method}:${e.params.resource ?? e.params.protocol}`
  : e.method)

beforeEach(() => {
  installStubs({ wgEnabled: true, ovpnEnabled: true, ikev2Enabled: true })
  sent.length = 0
  vi.mocked(control).mockReset().mockImplementation(answer(false))
  vi.mocked(controlApply).mockReset().mockImplementation(answer(true))
  vi.mocked(recordEvent).mockClear()
})

/** Вызовы control и controlApply в исходниках сервера: метод и имена полей запроса. */
function callSites() {
  const sites: { file: string, method: string, apply: boolean, keys: string[] | null }[] = []
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = `${dir}/${entry.name}`
      if (entry.isDirectory()) walk(path)
      else if (entry.name.endsWith('.ts') && !entry.name.endsWith('.test.ts') && entry.name !== 'control.ts') scan(path)
    }
  }
  const scan = (path: string) => {
    const source = ts.createSourceFile(path, readFileSync(path, 'utf8'), ts.ScriptTarget.Latest, true)
    const visit = (node: ts.Node) => {
      if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)
        && (node.expression.text === 'control' || node.expression.text === 'controlApply')) {
        const [method, params] = node.arguments
        const literal = params && ts.isObjectLiteralExpression(params)
          && params.properties.every(p => (ts.isPropertyAssignment(p) || ts.isShorthandPropertyAssignment(p)) && ts.isIdentifier(p.name))
        sites.push({
          file: path.slice(serverDir.length),
          method: method && ts.isStringLiteral(method) ? method.text : '',
          apply: node.expression.text === 'controlApply',
          keys: literal ? params.properties.map(p => (p.name as ts.Identifier).text).sort() : null,
        })
      }
      ts.forEachChild(node, visit)
    }
    visit(source)
  }
  walk(serverDir.replace(/\/$/, ''))
  return sites
}

describe('контракт control: исходники панели против записанных примеров', () => {
  const sites = callSites()

  it('вызовы найдены, метод и запрос везде записаны литералом', () => {
    expect(sites.length).toBeGreaterThanOrEqual(20)
    expect(sites.filter(s => !s.method || !s.keys)).toEqual([])
  })

  it('панель зовёт ровно те методы, на которые есть примеры', () => {
    expect([...new Set(sites.map(s => s.method))].sort()).toEqual([...new Set(examples.map(e => e.method))].sort())
  })

  it('*.apply идут через controlApply, остальное через control', () => {
    expect(sites.filter(s => s.apply !== s.method.endsWith('.apply'))).toEqual([])
  })

  it('поля каждого запроса совпадают с полями примера того же метода', () => {
    const known = (s: typeof sites[number]) => examples.some(e => e.method === s.method
      && isDeepStrictEqual(Object.keys(e.params).filter(k => !s.apply || k !== 'version').sort(), s.keys))
    expect(sites.filter(s => !known(s))).toEqual([])
  })
})

// Тестовый самоподписанный сертификат: CN=dev_12, серийный 1234ABCD.
const certDev12 = `-----BEGIN CERTIFICATE-----
MIIBaTCCAQ+gAwIBAgIEEjSrzTAKBggqhkjOPQQDAjARMQ8wDQYDVQQDDAZkZXZf
MTIwIBcNMjYxMTEwMTU0NDA2WhgPMjEyNjEwMTcxNTQ0MDZaMBExDzANBgNVBAMM
BmRldl8xMjBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABNylWHrxe4SwTKw+LD2y
9QWuOzVzrUNw8cuE2J+D7mAe01LZV3efbZ8R8duV+2OfExnhMBT42zywSRO23yVV
4n+jUzBRMB0GA1UdDgQWBBSKvzTSRIGx2KJ/jYKrWha0ub7pSjAfBgNVHSMEGDAW
gBSKvzTSRIGx2KJ/jYKrWha0ub7pSjAPBgNVHRMBAf8EBTADAQH/MAoGCCqGSM49
BAMCA0gAMEUCIC/1xsTAqUb11iAzSXl0XTE1tozPHfQyb3pLWm09Jt6iAiEAnG1o
n4qpQjeoJr1ooyLVvlUpWV00jLkyAYSfRP+Lars=
-----END CERTIFICATE-----`

const example = (method: string, pick: (params: Record<string, never>) => boolean = () => true) =>
  examples.find(e => e.method === method && pick(e.params as Record<string, never>))!
const date = (iso: string | null) => iso ? new Date(iso) : null

describe('контракт control: живые вызовы панели шлют записанные запросы и разбирают записанные ответы', () => {
  it('profile.read: ключ сервера WireGuard, готовность CA OpenVPN, режим IKEv2', async () => {
    expect(await loadServerKeys()).toEqual(example('profile.read', p => p.protocol === 'wg').result)
    expect(await caReady()).toBe(true)
    expect(await ikev2Mode()).toBe('letsencrypt')
    expect(sentMethods()).toEqual(['profile.read:wg', 'profile.read:openvpn', 'profile.read:ikev2'])
  })

  it('wg.apply: пиры из базы уходят записанным запросом', async () => {
    const peers = example('wg.apply').params.peers as { key: string, preshared_key: string, ip: string, expires_at: string | null }[]
    dbResults.push(peers.map(p => ({
      wgPublicKey: p.key, wgPresharedKey: p.preshared_key, wgIp: p.ip, deleting: false, frozenManual: false, expiresAt: date(p.expires_at),
    })))
    await syncWireguardConfig()
    expect(sentMethods()).toEqual(['wg.apply'])
  })

  it('ikev2.apply: учётные записи из базы уходят записанным запросом', async () => {
    const clients = example('ikev2.apply').params.clients as {
      device_id: number, username: string, password: string, ip: string, dns: string[], expires_at: string | null,
    }[]
    dbResults.push([], clients.map(c => ({
      id: c.device_id, username: c.username, password: c.password, ip: c.ip, deleting: false,
      frozenManual: false, expiresAt: date(c.expires_at), filterTraffic: c.dns.length === 1,
    })))
    await syncIkev2()
    expect(sentMethods()).toEqual(['ikev2.apply'])
  })

  it('openvpn.issue и openvpn.apply: выдача сертификата и запись ccd', async () => {
    const issued = example('openvpn.issue').result as { cert: string, key: string }
    const base = { id: 12, clientId: 7, deleting: false, ovpnCert: null, ovpnKey: null, ovpnIp: null }
    const withCert = { ...base, ovpnCert: issued.cert, ovpnKey: issued.key }
    const withIp = { ...withCert, ovpnIp: example('openvpn.apply').params.ip }
    dbResults.push([base], [{ id: 7, deleting: false, frozenManual: false, expiresAt: null, accessVersion: 3 }],
      [withCert], [withCert], [], [withIp], [{ frozenManual: false, expiresAt: null }])
    expect(await ensureDeviceOvpn(12)).toBe(withIp)
    expect(sentMethods()).toEqual(['openvpn.issue', 'openvpn.apply'])
  })

  it('openvpn.revoke и openvpn.disconnect: номер устройства и серийный номер из сертификата', async () => {
    await revokeAccessCert(certDev12)
    expect(await killOvpnClient('dev_12')).toBe(true)
    expect(sentMethods()).toEqual(['openvpn.revoke', 'openvpn.disconnect'])
  })

  it('state.read: сроки службы по трём протоколам совпали с базой - пересинхронизации нет', async () => {
    const wgKey = Object.keys((example('state.read', p => p.protocol === 'wg').result as { expires: object }).expires)[0]
    dbResults.push([{
      id: 12, wgPublicKey: wgKey, ovpnCert: 'test-cert', ikev2Username: 'user-12', deleting: false,
      frozenManual: false, expiresAt: new Date('2031-05-06T07:08:09.000Z'), clientDeleting: false,
    }])
    await syncControlExpiry()
    expect(sentMethods()).toEqual(['state.read:wg', 'state.read:openvpn', 'state.read:ikev2'])
  })

  it('expiry.run: снятие по сроку из ответа службы попадает в журнал', async () => {
    dbResults.push([], [{ clientId: 7, client: 'Anna', device: 'laptop' }])
    await reportControlExpiry()
    expect(sentMethods()).toEqual(['expiry.run'])
    expect(recordEvent).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      type: 'access_expired_offline', key: 'control-expiry:openvpn:12:2026-11-01T00:00:00.000Z',
      at: Date.parse('2026-11-01T00:00:05.000Z'), object: 'client:7', detail: 'OpenVPN',
    }))
  })

  it('settings.read и settings.apply: список выключенных экзитов читается и пишется отсортированным', async () => {
    expect(await toggleExit('ams', true, ['ams', 'fra', 'lon'])).toBe(true)
    expect(sentMethods()).toEqual(['settings.read:disabled-exits', 'settings.apply:disabled-exits'])
  })

  it('settings.apply: ручные маршруты уходят строками базы', async () => {
    const rows = example('settings.apply', p => p.resource === 'routes').params.config as unknown[]
    dbResults.push(rows)
    expect((await syncRoutesFile()).count).toBe(rows.length)
    expect(sentMethods()).toEqual(['settings.apply:routes'])
  })
})
