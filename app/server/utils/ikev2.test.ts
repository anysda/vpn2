import { beforeEach, describe, expect, it, vi } from 'vitest'
import { calls, dbOps, dbResults, installStubs, rejection, runtimeConfig } from '../test/stubs'
import {
  buildIkev2ClientInfo, ensureDeviceIkev2, ikev2CaReady, ikev2Mode, ikev2ServerReady, readIkev2CaPem, syncIkev2,
} from './ikev2'
import { control, controlApply } from './control'

vi.mock('../database/client', async () => ({
  ...(await import('../test/stubs')).databaseModule,
  IP_CLAIM_ATTEMPTS: 50,
  isUniqueViolation: (err: { code?: string }) => err?.code === 'SQLITE_CONSTRAINT_UNIQUE',
}))
vi.mock('./control', () => ({ control: vi.fn(), controlApply: vi.fn() }))

const failing = (err: Error) => Object.assign([], { then: (_ok: unknown, bad: (e: Error) => void) => bad(err) })
const unique = Object.assign(new Error('UNIQUE constraint failed: devices.ikev2_ip'), { code: 'SQLITE_CONSTRAINT_UNIQUE' })
const profile = (over: Record<string, unknown> = {}) => ({ ready: true, mode: 'self-signed', host: '203.0.113.7', domain: '', ca: '', ...over })
const answerProfile = (value: unknown) => vi.mocked(control).mockImplementation((async (name: string, args: unknown) => {
  calls.push(`control:${name}`)
  expect(args).toEqual({ protocol: 'ikev2' })
  return value
}) as never)
const lastSet = () => dbOps.filter(o => o.op === 'set').at(-1)!.args[0] as Record<string, string>

beforeEach(() => {
  installStubs()
  vi.mocked(control).mockReset()
  vi.mocked(controlApply).mockReset()
  vi.unstubAllEnvs()
})

describe('чтение профиля', () => {
  it('режим, готовность, наличие и текст CA берутся из profile.read', async () => {
    answerProfile(profile({ mode: 'letsencrypt', ready: false, ca: 'test-ca' }))
    expect(await ikev2Mode()).toBe('letsencrypt')
    expect(await ikev2ServerReady()).toBe(false)
    expect(await ikev2CaReady()).toBe(true)
    expect(await readIkev2CaPem()).toBe('test-ca')
    expect(control).toHaveBeenCalledWith('profile.read', { protocol: 'ikev2' })
  })

  it('пустая строка CA - CA не готова', async () => {
    answerProfile(profile({ ca: '' }))
    expect(await ikev2CaReady()).toBe(false)
  })
})

describe('ensureDeviceIkev2', () => {
  const empty = { id: 4, clientId: 7, name: 'Phone 1', ikev2Username: null, ikev2Password: null, ikev2Ip: null }
  const full = { ...empty, ikev2Username: 'anna-phone', ikev2Password: 'test-pass', ikev2Ip: '10.68.68.2' }

  it('устройства нет - ошибка с номером', async () => {
    dbResults.push([])
    expect((await rejection(ensureDeviceIkev2(4))).message).toBe('device 4 not found')
  })

  it('креды уже выданы - строка без записи', async () => {
    dbResults.push([full])
    expect(await ensureDeviceIkev2(4)).toBe(full)
    expect(calls).toEqual(['db:select'])
  })

  it('клиента нет - ошибка с номером клиента, записи нет', async () => {
    dbResults.push([empty], [])
    expect((await rejection(ensureDeviceIkev2(4))).message).toBe('client 7 not found')
    expect(calls).not.toContain('db:update')
  })

  it('выдача: логин из слагов клиента и устройства, пароль 20 символов alnum, адрес из пула', async () => {
    dbResults.push([empty], [{ name: 'Anna' }], [{ username: 'other-dev', ip: '10.68.68.2' }], [{ id: 4 }])
    await ensureDeviceIkev2(4)
    const set = lastSet()
    expect(set.ikev2Username).toBe('anna-phone-1')
    expect(set.ikev2Password).toMatch(/^[A-Za-z0-9]{20}$/)
    expect(set.ikev2Ip).toBe('10.68.68.3')
  })

  it('занятый логин получает числовой суффикс -2, затем -3', async () => {
    dbResults.push([empty], [{ name: 'Anna' }], [{ username: 'anna-phone-1', ip: null }, { username: 'anna-phone-1-2', ip: null }], [{ id: 4 }])
    await ensureDeviceIkev2(4)
    expect(lastSet().ikev2Username).toBe('anna-phone-1-3')
  })

  it('частично заполненное устройство сохраняет выданные логин и пароль', async () => {
    dbResults.push([{ ...empty, ikev2Username: 'anna-phone', ikev2Password: 'test-pass' }], [{ name: 'Anna' }], [], [{ id: 4 }])
    await ensureDeviceIkev2(4)
    expect([lastSet().ikev2Username, lastSet().ikev2Password]).toEqual(['anna-phone', 'test-pass'])
  })

  it('уже выданный адрес сохраняется, логин и пароль добираются', async () => {
    dbResults.push([{ ...empty, ikev2Ip: '10.68.68.9' }], [{ name: 'Anna' }], [{ username: null, ip: '10.68.68.9' }], [{ id: 4 }])
    await ensureDeviceIkev2(4)
    expect([lastSet().ikev2Ip, lastSet().ikev2Username]).toEqual(['10.68.68.9', 'anna-phone-1'])
  })

  it('запись ничего не вернула - перечитывается и берётся готовая строка', async () => {
    dbResults.push([empty], [{ name: 'Anna' }], [], [], [full])
    expect(await ensureDeviceIkev2(4)).toBe(full)
    expect(calls.filter(c => c === 'db:update')).toHaveLength(1)
  })

  it('конфликт UNIQUE - повтор с перечитанными занятыми значениями', async () => {
    dbResults.push([empty], [{ name: 'Anna' }], [], failing(unique), [empty], [{ name: 'Anna' }], [{ username: null, ip: '10.68.68.2' }], [{ id: 4 }])
    await ensureDeviceIkev2(4)
    expect(dbOps.filter(o => o.op === 'set').map(o => (o.args[0] as Record<string, string>).ikev2Ip)).toEqual(['10.68.68.2', '10.68.68.3'])
  })

  it('чужая ошибка записи не глотается', async () => {
    dbResults.push([empty], [{ name: 'Anna' }], [], failing(new Error('disk I/O error')))
    expect((await rejection(ensureDeviceIkev2(4))).message).toBe('disk I/O error')
  })

  it('50 конфликтов подряд - ошибка про логин и адрес IKEv2', async () => {
    for (let i = 0; i < 50; i++) dbResults.push([empty], [{ name: 'Anna' }], [], failing(unique))
    expect((await rejection(ensureDeviceIkev2(4))).message).toBe('device 4: не удалось занять логин и адрес IKEv2')
    expect(calls.filter(c => c === 'db:update')).toHaveLength(50)
  })
})

describe('buildIkev2ClientInfo', () => {
  const device = { id: 4, clientId: 7, name: 'phone', ikev2Username: 'anna-phone', ikev2Password: 'test-pass', ikev2Ip: '10.68.68.2' }

  it('self-signed: сервер из профиля, CA выдаётся, обращение к IP из аргумента не нужно', async () => {
    dbResults.push([device])
    answerProfile(profile({ ca: 'test-ca' }))
    expect(await buildIkev2ClientInfo(4, '198.51.100.1')).toEqual({
      server: '203.0.113.7', remoteId: '203.0.113.7', username: 'anna-phone', password: 'test-pass',
      caCertPem: 'test-ca', certificatePending: false,
    })
  })

  it('self-signed без CA - caCertPem null', async () => {
    dbResults.push([device])
    answerProfile(profile({ ca: '' }))
    expect((await buildIkev2ClientInfo(4, '')).caCertPem).toBeNull()
  })

  it('letsencrypt: CA клиенту не отдаётся, даже если она есть', async () => {
    dbResults.push([device])
    answerProfile(profile({ mode: 'letsencrypt', ca: 'test-ca', host: 'vpn.example.test', domain: 'vpn.example.test' }))
    const info = await buildIkev2ClientInfo(4, '')
    expect([info.caCertPem, info.certificatePending]).toEqual([null, false])
  })

  it.each([
    ['letsencrypt, хост не совпал с доменом', { mode: 'letsencrypt', host: '203.0.113.7', domain: 'vpn.example.test' }, true],
    ['self-signed, домен задан', { mode: 'self-signed', host: '203.0.113.7', domain: 'vpn.example.test' }, true],
    ['домен из одних пробелов', { mode: 'letsencrypt', host: '203.0.113.7', domain: '  ' }, false],
    ['домена нет', { mode: 'self-signed', host: '203.0.113.7', domain: '' }, false],
    ['letsencrypt, домен с пробелами по краям', { mode: 'letsencrypt', host: 'vpn.example.test', domain: ' vpn.example.test ' }, false],
  ])('certificatePending: %s', async (_name, over, pending) => {
    dbResults.push([device])
    answerProfile(profile(over))
    expect((await buildIkev2ClientInfo(4, '')).certificatePending).toBe(pending)
  })

  it('хост профиля пуст - берётся ENTRY_HOST, затем wgPublicHost', async () => {
    answerProfile(profile({ host: '' }))
    dbResults.push([device])
    vi.stubEnv('ENTRY_HOST', ' entry.example.test ')
    expect((await buildIkev2ClientInfo(4, '')).server).toBe('entry.example.test')
    vi.stubEnv('ENTRY_HOST', '')
    runtimeConfig.wgPublicHost = '203.0.113.50'
    dbResults.push([device])
    expect((await buildIkev2ClientInfo(4, '')).server).toBe('203.0.113.50')
  })
})

describe('syncIkev2', () => {
  const future = new Date(Date.now() + 86_400_000)
  const row = (id: number, over: Record<string, unknown> = {}) => ({
    id, username: `user-${id}`, deleting: false, password: `test-pass-${id}`, ip: `10.68.68.${id + 1}`,
    frozenManual: false, expiresAt: null, filterTraffic: true, ...over,
  })

  it('ikev2 выключен - ни базы, ни control', async () => {
    runtimeConfig.ikev2Enabled = false
    await syncIkev2()
    expect(calls).toEqual([])
  })

  it('в ikev2.apply идут только активные устройства с полными данными; dns и срок - по клиенту', async () => {
    runtimeConfig.ikev2Enabled = true
    dbResults.push([], [
      row(1),
      row(2, { filterTraffic: false, expiresAt: future }),
      row(3, { frozenManual: true }),
      row(4, { expiresAt: new Date(Date.now() - 1000) }),
      row(5, { deleting: true }),
      row(6, { username: null }),
      row(7, { password: null }),
      row(8, { ip: null }),
    ])
    await syncIkev2()
    expect(controlApply).toHaveBeenCalledOnce()
    expect(controlApply).toHaveBeenCalledWith('ikev2.apply', {
      clients: [
        { device_id: 1, username: 'user-1', password: 'test-pass-1', ip: '10.68.68.2', dns: ['10.99.0.1'], expires_at: null },
        { device_id: 2, username: 'user-2', password: 'test-pass-2', ip: '10.68.68.3', dns: ['8.8.8.8', '1.1.1.1'], expires_at: future.toISOString() },
      ],
    })
  })

  it('сирот нет - транзакции восстановления нет, запросов ровно два', async () => {
    runtimeConfig.ikev2Enabled = true
    await syncIkev2()
    expect(calls).toEqual(['db:select', 'db:select'])
  })

  it('сироты: недостающий адрес и логин выдаются, существующие и пароль сохраняются, полные не трогаются', async () => {
    runtimeConfig.ikev2Enabled = true
    const all = [
      { id: 1, name: 'phone', clientName: 'Anna', username: 'anna-phone', password: 'test-pass', ip: null },
      { id: 2, name: 'laptop', clientName: 'Anna', username: null, password: null, ip: '10.68.68.2' },
      { id: 3, name: 'tv', clientName: 'Boris', username: 'boris-tv', password: 'test-pass-3', ip: '10.68.68.5' },
    ]
    dbResults.push([{ id: 1 }, { id: 2 }], all, [], [], [])
    await syncIkev2()
    const sets = dbOps.filter(o => o.op === 'set').map(o => o.args[0] as Record<string, string>)
    expect(sets).toHaveLength(2)
    expect([sets[0]!.ikev2Username, sets[0]!.ikev2Ip, sets[0]!.ikev2Password]).toEqual(['anna-phone', '10.68.68.3', 'test-pass'])
    expect([sets[1]!.ikev2Username, sets[1]!.ikev2Ip]).toEqual(['anna-laptop', '10.68.68.2'])
    expect(sets[1]!.ikev2Password).toMatch(/^[A-Za-z0-9]{20}$/)
  })

  it('восстановление идёт до чтения устройств для apply', async () => {
    runtimeConfig.ikev2Enabled = true
    dbResults.push([{ id: 2 }], [{ id: 2, name: 'laptop', clientName: 'Anna', username: null, password: null, ip: '10.68.68.2' }], [])
    await syncIkev2()
    expect(calls.indexOf('db:update')).toBeLessThan(calls.lastIndexOf('db:select'))
    expect(calls.at(-1)).toBe('db:select')
  })
})
