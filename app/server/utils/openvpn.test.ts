import { beforeEach, describe, expect, it, vi } from 'vitest'
import { calls, dbOps, dbResults, installStubs, rejection, runtimeConfig } from '../test/stubs'
import {
  buildOvpnConfig, caReady, ensureDeviceOvpn, killOvpnClient, ovpnCn, reissueDeviceOvpn, revokeAccessCert, syncOpenvpnConfig,
} from './openvpn'
import { control, controlApply } from './control'

vi.mock('../database/client', async () => ({
  ...(await import('../test/stubs')).databaseModule,
  claimUnique: (await vi.importActual<typeof import('../database/client')>('../database/client')).claimUnique,
}))
vi.mock('./control', () => ({ control: vi.fn(), controlApply: vi.fn() }))

// Тестовые самоподписанные сертификаты: CN=dev_12, серийный 1234ABCD; CN=other.
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
const certOther = `-----BEGIN CERTIFICATE-----
MIIBZTCCAQqgAwIBAgIBdzAKBggqhkjOPQQDAjAQMQ4wDAYDVQQDDAVvdGhlcjAg
Fw0yNjExMTAxNTQ0MDZaGA8yMTI2MTAxNzE1NDQwNlowEDEOMAwGA1UEAwwFb3Ro
ZXIwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAATRp48ch8+4JLLSNIdpPlxmKJtk
kopPha/HX7/lPkw605RxjFuRNyHPZNTBw2TN4fcW980rvqFhy+kdUkxOfR3Ho1Mw
UTAdBgNVHQ4EFgQUFl4mOzxBD2gthka31UZfI7O1itMwHwYDVR0jBBgwFoAUFl4m
OzxBD2gthka31UZfI7O1itMwDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNJ
ADBGAiEA7BWuTvKR/PtD8P46j1cw01vJ6wB4lPYUM0kSh9wWUAoCIQCy3dk/qO2G
8nwPgmDofr6+Qy3mTvpFT+cdyCAk9IGrBg==
-----END CERTIFICATE-----`
const SHA256_EMPTY = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'

type Handlers = Record<string, unknown>
/** control отвечает по имени команды и пишет её в calls как control:<имя>; нет ответа - undefined. */
function routeControl(handlers: Handlers = {}) {
  const answer = (name: string) => {
    calls.push(`control:${name}`)
    const value = handlers[name]
    if (value instanceof Error) throw value
    return value
  }
  vi.mocked(control).mockImplementation(((name: string) => Promise.resolve().then(() => answer(name))) as never)
  vi.mocked(controlApply).mockImplementation(((name: string) => Promise.resolve().then(() => answer(name))) as never)
}

const active = { id: 7, deleting: false, frozenManual: false, expiresAt: null, accessVersion: 3 }
const baseRow = { id: 12, clientId: 7, deleting: false, ovpnCert: null, ovpnKey: null, ovpnIp: null }

beforeEach(() => {
  installStubs()
  vi.mocked(control).mockReset()
  vi.mocked(controlApply).mockReset()
  routeControl()
})

describe('ovpnCn, caReady, killOvpnClient, revokeAccessCert', () => {
  it('CN строится из номера устройства', () => {
    expect(ovpnCn(12)).toBe('dev_12')
  })

  it('caReady отдаёт ready из profile.read, ошибка связи не глотается', async () => {
    routeControl({ 'profile.read': { ready: true } })
    expect(await caReady()).toBe(true)
    expect(control).toHaveBeenCalledWith('profile.read', { protocol: 'openvpn' })
    routeControl({ 'profile.read': new Error('нет связи') })
    expect((await rejection(caReady())).message).toBe('нет связи')
  })

  it('killOvpnClient: номер устройства из CN и ответ killed', async () => {
    routeControl({ 'openvpn.disconnect': { killed: true } })
    expect(await killOvpnClient('dev_12')).toBe(true)
    expect(control).toHaveBeenCalledWith('openvpn.disconnect', { device_id: 12 })
  })

  it.each(['dev_', 'dev_1x', 'xdev_1', 'bob', 'dev_1\ndev_2'])('killOvpnClient("%s") - отказ без обращения к control', async (cn) => {
    expect((await rejection(killOvpnClient(cn))).message).toBe('Invalid managed device')
    expect(control).not.toHaveBeenCalled()
  })

  it('revokeAccessCert: устройство и серийный номер из сертификата', async () => {
    await revokeAccessCert(certDev12)
    expect(control).toHaveBeenCalledWith('openvpn.revoke', { device_id: 12, serial: '1234ABCD' })
  })

  it('revokeAccessCert: чужой CN и мусор вместо PEM - отказ, отзыва нет', () => {
    expect(() => revokeAccessCert(certOther)).toThrow('Invalid managed certificate')
    expect(() => revokeAccessCert('not a pem')).toThrow()
    expect(control).not.toHaveBeenCalled()
  })
})

describe('buildOvpnConfig', () => {
  const params = {
    clientCert: '\n test-cert \n', clientKey: ' test-key-a\n', serverHost: 'vpn.example.test', serverPort: 1194,
    proto: 'udp', dns: ['10.99.0.1', '8.8.8.8'], splitLocal: false,
  }
  const profile = { 'profile.read': { ca: ' test-ca\n', tlsCrypt: '\ntest-tls ' } }

  it('udp full-tunnel: весь текст, блоки обрезаны по краям', async () => {
    routeControl(profile)
    expect(await buildOvpnConfig(params)).toBe([
      'client', 'dev tun', 'proto udp', 'remote vpn.example.test 1194', 'resolv-retry infinite', 'nobind',
      'persist-key', 'persist-tun', 'block-ipv6', 'route-ipv6 ::/1', 'route-ipv6 8000::/1', 'remote-cert-tls server',
      'sndbuf 0', 'rcvbuf 0', 'fast-io', 'cipher AES-256-GCM', 'auth SHA256', 'verb 3',
      'dhcp-option DNS 10.99.0.1', 'dhcp-option DNS 8.8.8.8',
      '',
      '<ca>', 'test-ca', '</ca>', '<cert>', 'test-cert', '</cert>', '<key>', 'test-key-a', '</key>',
      '<tls-crypt>', 'test-tls', '</tls-crypt>', '',
    ].join('\n'))
    expect(control).toHaveBeenCalledWith('profile.read', { protocol: 'openvpn' })
  })

  it.each([
    ['udp4', 'udp', true], ['udp6', 'udp', true], ['tcp', 'tcp-client', false], ['tcp4', 'tcp-client', false], ['tcp-server', 'tcp-client', false], ['tcp6-server', 'tcp-client', false],
  ])('proto %s - клиентская строка "proto %s", fast-io: %s', async (proto, line, fastIo) => {
    routeControl(profile)
    const conf = await buildOvpnConfig({ ...params, proto })
    expect(conf.split('\n')).toContain(`proto ${line}`)
    expect(conf.split('\n').includes('fast-io')).toBe(fastIo)
  })

  it.each(['sctp', 'udp-server', 'tcp-client', 'UDP', 'udp46', ''])('proto "%s" не поддерживается - отказ до чтения профиля', async (proto) => {
    const err = await rejection(buildOvpnConfig({ ...params, proto }))
    expect(err.message).toBe(`unsupported OpenVPN protocol: ${proto}`)
    expect(control).not.toHaveBeenCalled()
  })

  it('split-local по умолчанию добавляет route-строки, mgmt-сеть идёт в туннель', async () => {
    routeControl(profile)
    const lines = (await buildOvpnConfig({ ...params, splitLocal: undefined })).split('\n')
    expect(lines).toContain('route 10.0.0.0 255.0.0.0 net_gateway')
    expect(lines).toContain('route 10.99.0.0 255.255.255.0 vpn_gateway')
  })

  it('без dns строк dhcp-option нет', async () => {
    routeControl(profile)
    expect(await buildOvpnConfig({ ...params, dns: [] })).not.toContain('dhcp-option')
  })
})

describe('ensureDeviceOvpn', () => {
  it('устройства нет - ошибка с номером', async () => {
    dbResults.push([])
    expect((await rejection(ensureDeviceOvpn(12))).message).toBe('device 12 not found')
  })

  it('сертификат, ключ и адрес есть - строка без записи и без control', async () => {
    const row = { ...baseRow, ovpnCert: 'test-cert', ovpnKey: 'test-key-a', ovpnIp: '10.67.67.2' }
    dbResults.push([row])
    expect(await ensureDeviceOvpn(12)).toBe(row)
    expect(calls).toEqual(['db:select'])
  })

  it.each([
    ['клиент заморожен', { ...active, frozenManual: true }, {}],
    ['клиент удаляется', { ...active, deleting: true }, {}],
    ['устройство удаляется', active, { deleting: true }],
    ['срок истёк', { ...active, expiresAt: new Date(Date.now() - 1000) }, {}],
  ])('выдача запрещена, когда %s: 409 и сертификат не выпускается', async (_name, owner, rowOver) => {
    dbResults.push([{ ...baseRow, ...rowOver }], [owner])
    const err = await rejection(ensureDeviceOvpn(12))
    expect([err.statusCode, err.message]).toEqual([409, 'Выдача сертификата недоступна до включения доступа'])
    expect(calls).not.toContain('control:openvpn.issue')
  })

  it('владельца нет - тот же 409', async () => {
    dbResults.push([baseRow], [])
    expect((await rejection(ensureDeviceOvpn(12))).statusCode).toBe(409)
  })

  it('выдача: порядок issue → запись сертификата → адрес → ccd allow', async () => {
    routeControl({ 'openvpn.issue': { cert: 'test-cert', key: 'test-key-a' } })
    const issued = { ...baseRow, ovpnCert: 'test-cert', ovpnKey: 'test-key-a' }
    const withIp = { ...issued, ovpnIp: '10.67.67.2' }
    dbResults.push([baseRow], [active], [issued], [issued], [], [withIp], [{ frozenManual: false, expiresAt: null }])
    expect(await ensureDeviceOvpn(12)).toBe(withIp)
    expect(control).toHaveBeenCalledWith('openvpn.issue', { device_id: 12, generation: SHA256_EMPTY })
    expect(dbOps.filter(o => o.op === 'set').map(o => Object.keys(o.args[0] as object).sort())).toEqual([
      ['ovpnCert', 'ovpnKey', 'updatedAt'], ['ovpnIp', 'updatedAt'],
    ])
    expect(calls.filter(c => c.startsWith('control:'))).toEqual(['control:openvpn.issue', 'control:openvpn.apply'])
    expect(calls.indexOf('control:openvpn.issue')).toBeLessThan(calls.indexOf('db:update'))
    expect(controlApply).toHaveBeenCalledWith('openvpn.apply', { device_id: 12, ip: '10.67.67.2', action: 'allow', expires_at: null })
  })

  it('запись сертификата не прошла (владелец сменил версию) - device not found', async () => {
    routeControl({ 'openvpn.issue': { cert: 'test-cert', key: 'test-key-a' } })
    dbResults.push([baseRow], [active], [])
    expect((await rejection(ensureDeviceOvpn(12))).message).toBe('device 12 not found')
    expect(controlApply).not.toHaveBeenCalled()
  })

  it('сертификат есть, адреса нет - issue не зовётся, адрес и ccd выдаются', async () => {
    const row = { ...baseRow, ovpnCert: 'test-cert', ovpnKey: 'test-key-a' }
    const withIp = { ...row, ovpnIp: '10.67.67.2' }
    dbResults.push([row], [row], [], [withIp], [{ frozenManual: false, expiresAt: null }])
    await ensureDeviceOvpn(12)
    expect(control).not.toHaveBeenCalled()
    expect(controlApply).toHaveBeenCalledWith('openvpn.apply', { device_id: 12, ip: '10.67.67.2', action: 'allow', expires_at: null })
  })

  it.each([
    ['клиент заморожен', [{ frozenManual: true, expiresAt: null }], {}, 'deny'],
    ['клиента в базе нет', [], {}, 'deny'],
    ['устройство удаляется', [{ frozenManual: false, expiresAt: null }], { deleting: true }, 'deny'],
  ])('ccd при выдаче: %s - %s', async (_name, clientRows, over, action) => {
    const full = { ...baseRow, ovpnCert: 'test-cert', ovpnKey: 'test-key-a', ovpnIp: '10.67.67.2' }
    // полная строка на входе вернулась бы сразу, поэтому у входной нет адреса
    const noIp = { ...full, ovpnIp: null }
    dbResults.push([noIp], [{ ...noIp }], [], [{ ...full, ...over }], clientRows)
    await ensureDeviceOvpn(12)
    expect(controlApply).toHaveBeenCalledWith('openvpn.apply', expect.objectContaining({ action }))
  })

  it('срок клиента уходит в ccd ISO-строкой', async () => {
    const expiresAt = new Date('2031-05-06T07:08:09.000Z')
    const row = { ...baseRow, ovpnCert: 'test-cert', ovpnKey: 'test-key-a', ovpnIp: null }
    dbResults.push([row], [row], [], [{ ...row, ovpnIp: '10.67.67.2' }], [{ frozenManual: false, expiresAt }])
    await ensureDeviceOvpn(12)
    expect(controlApply).toHaveBeenCalledWith('openvpn.apply', expect.objectContaining({ expires_at: '2031-05-06T07:08:09.000Z' }))
  })
})

describe('reissueDeviceOvpn', () => {
  const withCert = { ...baseRow, ovpnCert: certDev12, ovpnKey: 'test-key-old', ovpnIp: '10.67.67.2' }
  const handlers = {
    'openvpn.revoke': undefined, 'openvpn.disconnect': { killed: true }, 'profile.read': { ready: true },
    'openvpn.issue': { cert: 'test-cert-new', key: 'test-key-new' },
  }

  it('порядок: revoke → disconnect → issue → запись; поколение зависит от старого сертификата', async () => {
    routeControl(handlers)
    dbResults.push([{ id: 7 }], [withCert], [{ ...withCert, ovpnCert: 'test-cert-new' }])
    const updated = await reissueDeviceOvpn(12, 7, 3)
    expect(updated.ovpnCert).toBe('test-cert-new')
    expect(calls.filter(c => c.startsWith('control:'))).toEqual(['control:openvpn.revoke', 'control:openvpn.disconnect', 'control:openvpn.issue'])
    expect(calls.indexOf('control:openvpn.issue')).toBeLessThan(calls.lastIndexOf('db:update'))
    expect(control).toHaveBeenCalledWith('openvpn.revoke', { device_id: 12, serial: '1234ABCD' })
    expect(control).toHaveBeenCalledWith('openvpn.disconnect', { device_id: 12 })
    const issue = vi.mocked(control).mock.calls.find(c => c[0] === 'openvpn.issue')![1] as { generation: string }
    expect(issue.generation).toMatch(/^[0-9a-f]{64}$/)
    expect(issue.generation).not.toBe(SHA256_EMPTY)
  })

  it('отзыв не прошёл - выпуска и записи нет, наружу 503 ovpn_reissue_failed', async () => {
    routeControl({ ...handlers, 'openvpn.revoke': new Error('crl busy') })
    dbResults.push([{ id: 7 }], [withCert])
    const err = await rejection(reissueDeviceOvpn(12, 7, 3))
    expect([err.statusCode, err.statusMessage, err.message]).toEqual([503, 'ovpn_reissue_failed', 'Ошибка перевыпуска'])
    expect(calls).not.toContain('control:openvpn.issue')
    expect(calls).not.toContain('db:update')
  })

  it('версия клиента устарела - 503, устройство не читается', async () => {
    dbResults.push([])
    expect((await rejection(reissueDeviceOvpn(12, 7, 2))).statusMessage).toBe('ovpn_reissue_failed')
    expect(calls).toEqual(['db:select'])
  })

  it('устройства нет - 503', async () => {
    dbResults.push([{ id: 7 }], [])
    expect((await rejection(reissueDeviceOvpn(12, 7, 3))).statusCode).toBe(503)
  })

  it('сертификата не было и CA не готова - строка возвращается как есть, ничего не выпускается', async () => {
    routeControl({ ...handlers, 'profile.read': { ready: false } })
    dbResults.push([{ id: 7 }], [baseRow])
    expect(await reissueDeviceOvpn(12, 7, 3)).toBe(baseRow)
    expect(calls).not.toContain('control:openvpn.revoke')
    expect(calls).not.toContain('control:openvpn.issue')
  })

  it('сертификата не было, CA готова - выпуск с поколением от пустой строки', async () => {
    routeControl(handlers)
    dbResults.push([{ id: 7 }], [baseRow], [{ ...baseRow, ovpnCert: 'test-cert-new' }])
    await reissueDeviceOvpn(12, 7, 3)
    expect(control).toHaveBeenCalledWith('openvpn.issue', { device_id: 12, generation: SHA256_EMPTY })
    expect(calls).not.toContain('control:openvpn.revoke')
  })

  it('запись нового сертификата не прошла - 503', async () => {
    routeControl(handlers)
    dbResults.push([{ id: 7 }], [withCert], [])
    expect((await rejection(reissueDeviceOvpn(12, 7, 3))).statusMessage).toBe('ovpn_reissue_failed')
  })

  it('отказ одного перевыпуска не блокирует очередь: следующий идёт', async () => {
    routeControl(handlers)
    dbResults.push([], [{ id: 7 }], [baseRow], [{ ...baseRow, ovpnCert: 'test-cert-new' }])
    const first = rejection(reissueDeviceOvpn(12, 7, 1))
    const second = reissueDeviceOvpn(12, 7, 3)
    expect((await first).statusMessage).toBe('ovpn_reissue_failed')
    expect((await second).ovpnCert).toBe('test-cert-new')
  })
})

describe('syncOpenvpnConfig', () => {
  const row = (id: number, over: Record<string, unknown> = {}) => ({
    id, ovpnCert: 'test-cert', ovpnIp: `10.67.67.${id}`, deleting: false, frozenManual: false, expiresAt: null, ...over,
  })

  it('openvpn выключен - ни базы, ни control', async () => {
    runtimeConfig.ovpnEnabled = false
    await syncOpenvpnConfig()
    expect(calls).toEqual([])
  })

  it('CA не готова - ошибка, ccd не пишутся', async () => {
    runtimeConfig.ovpnEnabled = true
    routeControl({ 'profile.read': { ready: false } })
    expect((await rejection(syncOpenvpnConfig())).message).toBe('OpenVPN CA unavailable')
    expect(calls).toEqual(['control:profile.read'])
  })

  it('ccd по каждому устройству с сертификатом: allow и deny; без сертификата пропуск', async () => {
    runtimeConfig.ovpnEnabled = true
    routeControl({ 'profile.read': { ready: true }, 'state.read': { sessions: {} } })
    dbResults.push([row(1), row(2, { frozenManual: true }), row(3, { deleting: true }), row(4, { ovpnCert: null }), row(5, { expiresAt: new Date(Date.now() - 1000) })])
    await syncOpenvpnConfig()
    expect(vi.mocked(controlApply).mock.calls.map(c => [(c[1] as { device_id: number }).device_id, (c[1] as { action: string }).action])).toEqual([
      [1, 'allow'], [2, 'deny'], [3, 'deny'], [5, 'deny'],
    ])
  })

  it('нет адреса - выдаётся и пишется в ccd', async () => {
    runtimeConfig.ovpnEnabled = true
    routeControl({ 'profile.read': { ready: true }, 'state.read': { sessions: {} } })
    dbResults.push([row(1, { ovpnIp: null })], [{ id: 1, ovpnIp: null }], [], [{ id: 1, ovpnIp: '10.67.67.2' }])
    await syncOpenvpnConfig()
    expect(controlApply).toHaveBeenCalledWith('openvpn.apply', { device_id: 1, ip: '10.67.67.2', action: 'allow', expires_at: null })
  })

  it('сессии без ccd выбиваются после записи ccd; свои и чужие имена не трогаются', async () => {
    runtimeConfig.ovpnEnabled = true
    routeControl({
      'profile.read': { ready: true }, 'openvpn.disconnect': { killed: true },
      'state.read': { sessions: { dev_1: '10.67.67.1', dev_9: '10.67.67.9', dev_10: '10.67.67.10', bob: '10.67.67.30', dev_x: '10.67.67.31' } },
    })
    dbResults.push([row(1), row(10, { ovpnCert: null })])
    await syncOpenvpnConfig()
    expect(vi.mocked(control).mock.calls.filter(c => c[0] === 'openvpn.disconnect').map(c => c[1])).toEqual([{ device_id: 9 }, { device_id: 10 }])
    expect(calls.indexOf('control:openvpn.apply')).toBeLessThan(calls.indexOf('control:openvpn.disconnect'))
  })
})
