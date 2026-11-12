import { beforeEach, describe, expect, it, vi } from 'vitest'
import { calls, dbOps, dbResults, installStubs, rejection, runtimeConfig } from '../test/stubs'
import { buildWgClientConfig, ensureDeviceWg, loadServerKeys, syncWireguardConfig } from './wireguard'
import { control, controlApply } from './control'

vi.mock('../database/client', async () => ({
  ...(await import('../test/stubs')).databaseModule,
  claimUnique: (await vi.importActual<typeof import('../database/client')>('../database/client')).claimUnique,
}))
vi.mock('./control', () => ({ control: vi.fn(), controlApply: vi.fn() }))

const base = {
  clientPrivateKey: 'test-key-a',
  clientPresharedKey: 'test-key-b',
  clientIp: '10.66.66.5',
  serverPublicKey: 'test-key-c',
  serverEndpoint: '203.0.113.7',
  serverPort: 51820,
}
/** Элемент очереди dbResults, на котором await цепочки отклоняется. */
const failing = (err: Error) => Object.assign([], { then: (_ok: unknown, bad: (e: Error) => void) => bad(err) })
const unique = Object.assign(new Error('UNIQUE constraint failed: devices.wg_ip'), { code: 'SQLITE_CONSTRAINT_UNIQUE' })

beforeEach(() => {
  installStubs()
  vi.mocked(control).mockReset()
  vi.mocked(controlApply).mockReset()
  vi.unstubAllEnvs()
})

describe('buildWgClientConfig', () => {
  it('full-tunnel: все строки конфига и значения по умолчанию', () => {
    const conf = buildWgClientConfig({ ...base, splitLocal: false })
    expect(conf).toBe([
      '[Interface]',
      'PrivateKey = test-key-a',
      'Address = 10.66.66.5/24, fd66:66::5/64',
      'DNS = 10.99.0.1',
      'MTU = 1420',
      '',
      '[Peer]',
      'PublicKey = test-key-c',
      'PresharedKey = test-key-b',
      'Endpoint = 203.0.113.7:51820',
      'AllowedIPs = 0.0.0.0/0, ::/0',
      'PersistentKeepalive = 25',
      '',
    ].join('\n'))
  })

  it('свои dns и mtu подменяют значения по умолчанию', () => {
    const conf = buildWgClientConfig({ ...base, splitLocal: false, dns: '8.8.8.8', mtu: 1280 })
    expect(conf).toContain('\nDNS = 8.8.8.8\n')
    expect(conf).toContain('\nMTU = 1280\n')
  })

  it('адрес из новой /24 получает ULA с третьим октетом, старой - без него', () => {
    expect(buildWgClientConfig({ ...base, clientIp: '10.66.67.9', splitLocal: false }))
      .toContain('Address = 10.66.67.9/24, fd66:66::67:9/64')
    expect(buildWgClientConfig({ ...base, clientIp: '10.66.66.200', splitLocal: false }))
      .toContain('Address = 10.66.66.200/24, fd66:66::200/64')
  })

  it('адрес не v4 - ошибка разбора', () => {
    expect(() => buildWgClientConfig({ ...base, clientIp: 'not-an-ip' })).toThrow('wgUlaAddress: не разобран v4-адрес "not-an-ip"')
  })

  it('split-local по умолчанию: не full-tunnel, mgmt и wg-сегмент идут в туннель', () => {
    const line = buildWgClientConfig(base).split('\n').find(l => l.startsWith('AllowedIPs = '))!
    expect(line).not.toContain('0.0.0.0/0')
    expect(line).toContain('10.66.66.0/24')
    expect(line).toContain('10.99.0.0/24')
  })

  it('свои tunnelPrefixes заменяют список по умолчанию', () => {
    const line = buildWgClientConfig({ ...base, tunnelPrefixes: ['10.77.7.'] }).split('\n').find(l => l.startsWith('AllowedIPs = '))!
    expect(line).toContain('10.77.7.0/24')
    expect(line).not.toContain('10.99.0.0/24')
  })

  it('IPv6-вход заменяется на IPv4 входа из NUXT_WG_ENDPOINT_IP', () => {
    vi.stubEnv('NUXT_WG_ENDPOINT_IP', '203.0.113.9')
    expect(buildWgClientConfig({ ...base, serverEndpoint: '2001:db8::1', splitLocal: false }))
      .toContain('Endpoint = 203.0.113.9:51820')
  })

  it('имя хоста остаётся именем в Endpoint', () => {
    expect(buildWgClientConfig({ ...base, serverEndpoint: 'vpn.example.test', splitLocal: false }))
      .toContain('Endpoint = vpn.example.test:51820')
  })
})

describe('loadServerKeys', () => {
  it('берёт публичный ключ через profile.read для wg', async () => {
    vi.mocked(control).mockResolvedValue({ publicKey: 'test-key-c' })
    expect(await loadServerKeys()).toEqual({ publicKey: 'test-key-c' })
    expect(control).toHaveBeenCalledWith('profile.read', { protocol: 'wg' })
  })
})

describe('ensureDeviceWg', () => {
  const full = { id: 4, wgPrivateKey: 'test-key-a', wgPublicKey: 'test-key-b', wgPresharedKey: 'test-key-c', wgIp: '10.66.66.2' }

  it('устройства нет - ошибка с номером', async () => {
    dbResults.push([])
    expect((await rejection(ensureDeviceWg(4))).message).toBe('device 4 not found')
  })

  it('ключи и адрес уже есть - строка возвращается без записи', async () => {
    dbResults.push([full])
    expect(await ensureDeviceWg(4)).toBe(full)
    expect(calls).toEqual(['db:select'])
  })

  it('пустое устройство получает адрес из пула, три ключа base64 и запись по isNull(wg_ip)', async () => {
    dbResults.push([{ id: 4, wgPrivateKey: null, wgPublicKey: null, wgPresharedKey: null, wgIp: null }], [{ ip: '10.66.66.2' }], [{ id: 4 }])
    await ensureDeviceWg(4)
    const set = dbOps.find(o => o.op === 'set')!.args[0] as Record<string, string>
    expect(set.wgIp).toBe('10.66.66.3')
    for (const key of ['wgPrivateKey', 'wgPublicKey', 'wgPresharedKey']) expect(set[key]).toMatch(/^[A-Za-z0-9+/]{43}=$/)
    expect(set.wgPrivateKey).not.toBe(set.wgPublicKey)
  })

  it('частично заполненное устройство сохраняет уже выданные значения', async () => {
    dbResults.push([{ id: 4, wgPrivateKey: 'test-key-a', wgPublicKey: 'test-key-b', wgPresharedKey: null, wgIp: '10.66.66.8' }], [{ id: 4 }])
    await ensureDeviceWg(4)
    const set = dbOps.find(o => o.op === 'set')!.args[0] as Record<string, string>
    expect([set.wgPrivateKey, set.wgPublicKey, set.wgIp]).toEqual(['test-key-a', 'test-key-b', '10.66.66.8'])
    expect(set.wgPresharedKey).toMatch(/^[A-Za-z0-9+/]{43}=$/)
  })

  it('условная запись ничего не вернула - строку перечитывают и берут готовую', async () => {
    dbResults.push([{ id: 4, wgPrivateKey: null, wgPublicKey: null, wgPresharedKey: null, wgIp: null }], [], [], [full])
    expect(await ensureDeviceWg(4)).toBe(full)
    expect(calls.filter(c => c === 'db:update')).toHaveLength(1)
  })

  const empty = { id: 4, wgPrivateKey: null, wgPublicKey: null, wgPresharedKey: null, wgIp: null }

  it('конфликт UNIQUE - новая попытка берёт следующий адрес', async () => {
    dbResults.push([empty], [{ ip: '10.66.66.2' }], failing(unique), [empty], [{ ip: '10.66.66.2' }, { ip: '10.66.66.3' }], [{ id: 4, wgIp: '10.66.66.4' }])
    expect(await ensureDeviceWg(4)).toEqual({ id: 4, wgIp: '10.66.66.4' })
    expect(dbOps.filter(o => o.op === 'set').map(o => (o.args[0] as { wgIp: string }).wgIp)).toEqual(['10.66.66.3', '10.66.66.4'])
  })

  it('чужая ошибка записи не глотается', async () => {
    dbResults.push([empty], [], failing(new Error('disk I/O error')))
    expect((await rejection(ensureDeviceWg(4))).message).toBe('disk I/O error')
    expect(calls.filter(c => c === 'db:update')).toHaveLength(1)
  })

  it('50 конфликтов подряд - ошибка про адрес WG', async () => {
    for (let i = 0; i < 50; i++) dbResults.push([empty], [], failing(unique))
    expect((await rejection(ensureDeviceWg(4))).message).toBe('device 4: не удалось занять адрес WG')
    expect(calls.filter(c => c === 'db:update')).toHaveLength(50)
  })
})

describe('syncWireguardConfig', () => {
  const future = new Date(Date.now() + 86_400_000)
  const peer = (n: number, over: Record<string, unknown> = {}) => ({
    wgPublicKey: `test-key-pub-${n}`, wgPresharedKey: `test-key-psk-${n}`, wgIp: `10.66.66.${n}`,
    deleting: false, frozenManual: false, expiresAt: null, ...over,
  })

  it('wg выключен - ни базы, ни control', async () => {
    runtimeConfig.wgEnabled = false
    await syncWireguardConfig()
    expect(calls).toEqual([])
    expect(controlApply).not.toHaveBeenCalled()
  })

  it('в wg.apply идут только активные пиры с полными данными; срок - ISO или null', async () => {
    runtimeConfig.wgEnabled = true
    dbResults.push([
      peer(2),
      peer(3, { expiresAt: future }),
      peer(4, { frozenManual: true }),
      peer(5, { expiresAt: new Date(Date.now() - 1000) }),
      peer(6, { deleting: true }),
      peer(7, { wgPublicKey: null }),
      peer(8, { wgPresharedKey: null }),
      peer(9, { wgIp: null }),
    ])
    await syncWireguardConfig()
    expect(controlApply).toHaveBeenCalledOnce()
    expect(controlApply).toHaveBeenCalledWith('wg.apply', {
      peers: [
        { key: 'test-key-pub-2', preshared_key: 'test-key-psk-2', ip: '10.66.66.2', expires_at: null },
        { key: 'test-key-pub-3', preshared_key: 'test-key-psk-3', ip: '10.66.66.3', expires_at: future.toISOString() },
      ],
    })
  })

  it('нет устройств - wg.apply с пустым списком пиров', async () => {
    runtimeConfig.wgEnabled = true
    await syncWireguardConfig()
    expect(controlApply).toHaveBeenCalledWith('wg.apply', { peers: [] })
  })
})
