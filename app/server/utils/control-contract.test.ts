import { readdirSync, readFileSync } from 'node:fs'
import { isDeepStrictEqual } from 'node:util'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { dbOps, dbResults, fakeEvent, installStubs } from '../test/stubs'
import { control, controlApply } from './control'
import { recordEvent } from './events'
import { loadServerKeys, syncWireguardConfig } from './wireguard'
import { buildOvpnConfig, caReady, ensureDeviceOvpn, killOvpnClient, revokeAccessCert } from './openvpn'
import { buildIkev2ClientInfo, ikev2Mode, ikev2ServerReady, readIkev2CaPem, syncIkev2 } from './ikev2'
import { reportControlExpiry, syncControlExpiry } from './expiry'
import { toggleExit } from './exit-control'
import { syncRoutesFile } from './routes-sync'
import { accessAdapter } from './access-adapter'
import { collectTraffic } from './traffic-collector'
import { syncClientPrefsFile } from './client-prefs'
import routeCheck from '../api/routes/check.get'

vi.hoisted(() => vi.stubGlobal('defineEventHandler', (h: unknown) => h))

// Примеры запросов и ответов control - общие со службой: те же запросы
// infra/vpn2/tests/test_control.py пропускает через проверку contract.py, а ответы
// настоящего исполнителя сверяет с записанными infra/vpn2/tests/test_control_replies.py.
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
vi.mock('./auth', async () => (await import('../test/stubs')).authModule)
vi.mock('./control', () => ({ control: vi.fn(), controlApply: vi.fn() }))
vi.mock('./access-lock', () => ({ withAccessLock: (action: () => Promise<void>) => action() }))
vi.mock('./events', async importOriginal => ({ ...(await importOriginal<object>()), recordEvent: vi.fn(async () => true) }))

const sent: Example[] = []
const label = (e: Example) => e.method === 'settings.apply' || e.method.endsWith('.read')
  ? `${e.method}:${e.params.resource ?? e.params.protocol}`
  : e.method

// Учёт чтения: записанный ответ уходит панели в обёртке, которая помнит каждое прочитанное поле
// (путь от корня ответа, элементы списков свёрнуты в []) и каждое поле, которого в ответе нет.
// Поле, переименованное или добавленное в примере, панель не прочтёт - последний блок файла это ловит.
const read = new Map<string, Set<string>>()
const absent = new Set<string>()
const ownData = (target: object, key: string | symbol): key is string =>
  typeof key === 'string' && (Array.isArray(target) ? /^\d+$/.test(key) : Object.hasOwn(target, key))
function tracked<T>(value: T, name: string, path: string): T {
  if (value === null || typeof value !== 'object') return value
  const seen = read.get(name) ?? read.set(name, new Set()).get(name)!
  const at = (target: object, key: string) => Array.isArray(target) ? `${path}[]` : path ? `${path}.${key}` : key
  const leaf = (target: object, key: string) => {
    if (typeof Reflect.get(target, key) !== 'object') seen.add(at(target, key))
  }
  return new Proxy(value, {
    get(target, key, receiver) {
      const item: unknown = Reflect.get(target, key, receiver)
      if (ownData(target, key)) {
        leaf(target, key)
        return tracked(item, name, at(target, key))
      }
      // then спрашивает await, toJSON - JSON.stringify: это не поля ответа.
      if (typeof key === 'string' && !(key in target) && key !== 'then' && key !== 'toJSON') absent.add(`${name}: ${at(target, key)}`)
      return item
    },
    has(target, key) {
      if (ownData(target, key)) leaf(target, key)
      else if (typeof key === 'string' && !(key in target)) absent.add(`${name}: ${at(target, key)}`)
      return Reflect.has(target, key)
    },
    // Перечисление имён (Object.keys, entries, in-циклы) читает имена полей: для словарей
    // «ключ - значение» имя и есть данные.
    ownKeys(target) {
      for (const key of Object.keys(target)) leaf(target, key)
      return Reflect.ownKeys(target)
    },
  })
}
/** Все поля записанного ответа теми же путями, что в учёте чтения, и тип каждого. */
function leaves(value: unknown, path = ''): [string, string][] {
  if (value === null || typeof value !== 'object') return [[path, value === null ? 'null' : typeof value]]
  if (Array.isArray(value)) return value.flatMap(item => leaves(item, `${path}[]`))
  return Object.entries(value).flatMap(([key, item]) => leaves(item, path ? `${path}.${key}` : key))
}

/** Отвечает записанным примером; запроса, которого в примерах нет, панель слать не должна. */
function answer(apply: boolean) {
  return (async (method: string, params: Record<string, unknown>) => {
    const wire = apply ? { ...params, version: 1 } : params
    const example = examples.find(e => e.method === method && isDeepStrictEqual(e.params, wire))
    if (!example) throw new Error(`нет записанного примера: ${method} ${JSON.stringify(wire)}`)
    sent.push(example)
    return tracked(example.result, label(example), '')
  }) as never
}
const sentMethods = () => sent.map(label)

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
// Клиент и операция доступа для сверок accessAdapter.read: срок тот же, что в записанных ответах.
const accessClient = { id: 7, accessVersion: 3, deleting: false, frozenManual: false, filterTraffic: false, expiresAt: new Date('2031-05-06T07:08:09.000Z') }
const accessOperation = { clientId: 7, version: 3, kind: 'update', deviceId: null, protocols: { openvpn: { confirmedVersion: null } } } as never
const noPayload = { retired: [], rotateOvpn: [] } as never

describe('контракт control: живые вызовы панели шлют записанные запросы и разбирают записанные ответы', () => {
  it('profile.read: ключ сервера WireGuard, готовность CA OpenVPN, режим IKEv2', async () => {
    expect((await loadServerKeys()).publicKey).toBe('AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=')
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

  it('state.read и openvpn.certificate: проверка операции доступа сверяет сертификат устройства', async () => {
    dbResults.push([accessClient], [{ id: 12, clientId: 7, deleting: false, ovpnCert: certDev12 }])
    expect(await accessAdapter.read('openvpn', accessOperation, noPayload)).toBe(true)
    expect(sentMethods()).toEqual(['state.read:openvpn', 'openvpn.certificate'])
  })

  it('traffic.read: счётчики трёх протоколов разбираются по устройствам базы', async () => {
    const [wg] = (example('traffic.read', p => p.protocol === 'wg').result as { samples: { key: string }[] }).samples
    const [ikev2] = (example('traffic.read', p => p.protocol === 'ikev2').result as { samples: { username: string, uniqueid: string, rx: number, tx: number }[] }).samples
    dbResults.push([{ id: 12, pk: wg!.key, ikev2: ikev2!.username }], [])
    await collectTraffic()
    expect(sentMethods().sort()).toEqual(['traffic.read:ikev2', 'traffic.read:openvpn', 'traffic.read:wg'])
    // Счётчик IKEv2 из ответа лёг в снимок сборщика под устройством из базы.
    const saved = dbOps.filter(o => o.op === 'values').map(o => o.args[0] as { name?: string, value?: string }).find(v => v.name === 'ikev2')
    expect(JSON.parse(saved!.value!)).toEqual({ [`12:${ikev2!.uniqueid}`]: [ikev2!.rx, ikev2!.tx] })
  })

  it('routes.plan: проверка маршрута собирает план из записанного ответа', async () => {
    dbResults.push([])
    const reply = await routeCheck(fakeEvent({}, { q: '1.1.1.1' })) as { query: string, ip: string }
    expect(sentMethods()).toEqual(['routes.plan'])
    expect(reply).toMatchObject({ query: '1.1.1.1', ip: '1.1.1.1' })
    expect(JSON.stringify(reply)).toContain('direct-ru')
  })

  it('settings.apply: предпочтения экзитов уходят отсортированными адресами по узлам', async () => {
    dbResults.push([
      { node: 'fra', wg: '10.67.67.2', ovpn: null, ikev2: null },
      { node: 'fra', wg: null, ovpn: '10.66.66.2', ikev2: null },
    ])
    await syncClientPrefsFile()
    expect(sentMethods()).toEqual(['settings.apply:preferences'])
  })

  it('profile.read: профиль OpenVPN несёт CA и ключ tls-crypt из ответа', async () => {
    const config = await buildOvpnConfig({
      clientCert: 'client-cert', clientKey: 'client-key', serverHost: '203.0.113.7', serverPort: 1194, proto: 'udp', dns: ['10.99.0.1'],
    })
    expect(config).toContain('<ca>\ntest-ca\n</ca>')
    expect(config).toContain('<tls-crypt>\ntest-tls-crypt\n</tls-crypt>')
    expect(sentMethods()).toEqual(['profile.read:openvpn'])
  })

  it('profile.read: реквизиты IKEv2 берут адрес, домен, готовность и CA из ответа', async () => {
    dbResults.push([{ id: 12, ikev2Username: 'user-12', ikev2Password: 'test-pass-0012', ikev2Ip: '10.68.68.2' }])
    expect(await buildIkev2ClientInfo(12)).toEqual({
      server: '203.0.113.7', remoteId: '203.0.113.7', username: 'user-12', password: 'test-pass-0012',
      caCertPem: null, certificatePending: true,
    })
    expect(await ikev2ServerReady()).toBe(true)
    expect(await readIkev2CaPem()).toBe('')
    expect(new Set(sentMethods())).toEqual(new Set(['profile.read:ikev2']))
  })

  it('state.read: пир WireGuard на адресе устройства сверяется по ключу и адресам', async () => {
    const state = example('state.read', p => p.protocol === 'wg').result as { peers: Record<string, string[]> }
    const [key, addresses] = Object.entries(state.peers)[0]!
    const device = { id: 12, clientId: 7, deleting: false, wgPublicKey: key, wgIp: addresses[0]!.replace('/32', '') }
    dbResults.push([accessClient], [device], [accessClient], [{ ...device, wgPublicKey: 'other-key' }])
    expect(await accessAdapter.read('wg', accessOperation, noPayload)).toBe(true)
    // На адресе устройства чужой ключ: адреса пиров из ответа это показывают.
    expect(await accessAdapter.read('wg', accessOperation, noPayload)).toBe(false)
    expect(sentMethods()).toEqual(['state.read:wg', 'state.read:wg'])
  })

  it('state.read: учётная запись IKEv2 сверяется по устройству, отпечатку и сессиям', async () => {
    const live = { id: 12, clientId: 7, deleting: false, ikev2Username: 'user-12', ikev2Password: 'test-pass-0012', ikev2Ip: '10.68.68.2' }
    const gone = { id: 13, clientId: 7, deleting: true, ikev2Username: 'user-13', ikev2Password: 'test-pass-0013', ikev2Ip: '10.68.68.3' }
    dbResults.push([{ ...accessClient, filterTraffic: true }], [live, gone],
      [{ ...accessClient, filterTraffic: true }], [{ ...live, deleting: true }])
    expect(await accessAdapter.read('ikev2', accessOperation, noPayload)).toBe(true)
    // Снятое устройство с живой сессией в ответе - доступ ещё не снят.
    expect(await accessAdapter.read('ikev2', accessOperation, noPayload)).toBe(false)
    expect(sentMethods()).toEqual(['state.read:ikev2', 'state.read:ikev2'])
  })

  it('openvpn.certificate: перевыпуск ждёт годного сертификата, отзыв - отозванного', async () => {
    const device = { id: 12, clientId: 7, deleting: false, ovpnCert: certDev12 }
    dbResults.push([accessClient], [device], [accessClient], [device])
    expect(await accessAdapter.read('openvpn', accessOperation, { retired: [], rotateOvpn: [{ id: 12, cert: 'old-cert' }] } as never)).toBe(true)
    const retired = [{ id: 12, wgPublicKey: null, ovpnCert: certDev12 }]
    expect(await accessAdapter.read('openvpn', accessOperation, { retired, rotateOvpn: [] } as never)).toBe(false)
    expect(new Set(sentMethods())).toEqual(new Set(['state.read:openvpn', 'openvpn.certificate']))
  })
})

// Стоит последним: считает чтения всех живых вызовов выше.
describe('контракт control: каждое поле записанных ответов панель читает', () => {
  const a = 'AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8='
  const b = 'AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA='
  // Поля ответов, как их знает панель: путь и тип. Минус перед типом - панель поле не читает
  // (служба его отдаёт, форму держит infra/vpn2/tests/test_control_replies.py).
  const fields: Record<string, Record<string, string>> = {
    'profile.read:wg': { publicKey: 'string' },
    'profile.read:openvpn': { ready: 'boolean', ca: 'string', tlsCrypt: 'string' },
    'profile.read:ikev2': { ready: 'boolean', mode: 'string', host: 'string', domain: 'string', ca: 'string' },
    'state.read:wg': { [`peers.${a}[]`]: 'string', [`expires.${a}`]: 'string' },
    'state.read:openvpn': { 'sessions.dev_12': 'string', 'expires.12': 'string' },
    'state.read:ikev2': {
      'devices[]': 'number', 'sas[].id': '-number', 'sas[].username': 'string', 'credentials.user-12': 'string', 'expires.12': 'string',
    },
    'traffic.read:wg': { 'samples[].key': 'string', 'samples[].rx': 'number', 'samples[].tx': 'number' },
    'traffic.read:openvpn': { 'samples[].device_id': 'number', 'samples[].rx': 'number', 'samples[].tx': 'number' },
    'traffic.read:ikev2': { 'samples[].uniqueid': 'string', 'samples[].username': 'string', 'samples[].rx': 'number', 'samples[].tx': 'number' },
    'wg.apply': { 'version': '-number', [`observed.peers.${a}[]`]: '-string', [`observed.peers.${b}[]`]: '-string' },
    'ikev2.apply': {
      'version': '-number', 'observed.devices[]': '-number', 'observed.sas[].id': '-number', 'observed.sas[].username': '-string',
      'observed.credentials.user-1': '-string', 'observed.credentials.user-2': '-string',
    },
    'openvpn.apply': {
      'version': '-number', 'observed.device_id': '-number', 'observed.allowed': '-boolean', 'observed.sessions': '-number',
      'observed.revoked': '-boolean',
    },
    'openvpn.issue': { cert: 'string', key: 'string' },
    'openvpn.revoke': { revoked: '-boolean' },
    'openvpn.disconnect': { killed: 'boolean' },
    'openvpn.certificate': { valid: 'boolean', revoked: 'boolean', allowed: 'boolean', present: 'boolean' },
    'settings.read:disabled-exits': { 'config.disabled[]': 'string' },
    'settings.apply:disabled-exits': { 'config.disabled[]': '-string' },
    'settings.apply:routes': { 'config[].type': '-string', 'config[].value': '-string', 'config[].outbound': '-string' },
    'settings.apply:preferences': { 'config.fra[]': '-string' },
    'routes.plan': { revision: 'string', size: 'number', data: 'string' },
    'expiry.run': {
      'removed[].protocol': 'string', 'removed[].id': 'number', 'removed[].expires_at': 'string', 'removed[].at': 'string',
      'latest[].protocol': '-string', 'latest[].id': '-number', 'latest[].expires_at': '-string', 'failed[]': '-string',
    },
  }

  it('у каждого поля записанного ответа ожидаемые имя и тип, прочитано всё, что панели нужно', () => {
    const seen = Object.fromEntries(examples.map(e => [label(e), Object.fromEntries(
      leaves(e.result).map(([path, type]) => [path, (read.get(label(e))?.has(path) ? '' : '-') + type]))]))
    expect(seen).toEqual(fields)
  })

  it('полей, которых в записанных ответах нет, панель не читает', () => {
    expect([...absent]).toEqual([])
  })
})
