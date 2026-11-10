import { describe, expect, it } from 'vitest'
import { ovpnLocalRoutes, wgAllowedIps } from './allowed-ips'

const ipToInt = (ip: string): number => ip.split('.').reduce((acc, o) => acc * 256 + Number(o), 0)

function v4Nets(line: string): Array<{ start: number, end: number, cidr: string }> {
  return line.split(', ').filter(c => /^\d/.test(c) && c.includes('.')).map((cidr) => {
    const [base, len] = cidr.split('/')
    const start = ipToInt(base!)
    return { start, end: start + 2 ** (32 - Number(len)) - 1, cidr }
  })
}

const covered = (line: string, ip: string): boolean => {
  const n = ipToInt(ip)
  return v4Nets(line).some(r => n >= r.start && n <= r.end)
}

describe('wgAllowedIps', () => {
  it('без сплита - классический full-tunnel, остальные аргументы игнорируются', () => {
    expect(wgAllowedIps(false)).toBe('0.0.0.0/0, ::/0')
    expect(wgAllowedIps(false, ['10.66.66.'], '193.1.2.3', '1.2.3.4')).toBe('0.0.0.0/0, ::/0')
  })

  it('сплит: локальные диапазоны исключены, публичные на месте', () => {
    const line = wgAllowedIps(true)
    for (const ip of ['10.0.0.1', '10.255.255.255', '192.168.1.1', '172.16.0.1', '172.31.255.255',
      '100.64.0.1', '100.100.0.5', '100.127.255.255', '127.0.0.1', '0.0.0.0', '224.0.0.251', '255.255.255.255']) {
      expect(covered(line, ip), ip).toBe(false)
    }
    for (const ip of ['1.1.1.1', '8.8.8.8', '11.0.0.1', '100.63.255.255', '100.128.0.1', '172.15.255.255',
      '172.32.0.1', '192.167.255.255', '192.169.0.1', '169.254.1.1', '223.255.255.255']) {
      expect(covered(line, ip), ip).toBe(true)
    }
  })

  it('сплит: префиксы не пересекаются и идут по возрастанию', () => {
    const nets = v4Nets(wgAllowedIps(true))
    for (let i = 1; i < nets.length; i++) expect(nets[i]!.start).toBeGreaterThan(nets[i - 1]!.end)
  })

  it('сплит: v6 - 2000::/3 и ULA fd66:66::/64 в конце', () => {
    const parts = wgAllowedIps(true).split(', ')
    expect(parts.slice(-2)).toEqual(['2000::/3', 'fd66:66::/64'])
    expect(parts).not.toContain('::/0')
  })

  it('tunnelPrefixes превращаются в /24 между публичными и v6; битые и пустые отбрасываются', () => {
    const parts = wgAllowedIps(true, ['10.66.66.', '10.99.0.', '', 'abc', '10.66.', '10.66.66.1', ' 10.5.5. ']).split(', ')
    expect(parts.slice(-5)).toEqual(['10.66.66.0/24', '10.99.0.0/24', '10.5.5.0/24', '2000::/3', 'fd66:66::/64'])
  })

  it('endpoint-IP выбивается: ровно /32 не покрыт, соседи покрыты, префикс разбит', () => {
    const base = wgAllowedIps(true)
    const line = wgAllowedIps(true, [], '193.5.6.7')
    expect(covered(base, '193.5.6.7')).toBe(true)
    expect(covered(line, '193.5.6.7')).toBe(false)
    expect(covered(line, '193.5.6.6')).toBe(true)
    expect(covered(line, '193.5.6.8')).toBe(true)
    expect(covered(line, '193.0.0.0')).toBe(true)
    expect(covered(line, '193.255.255.255')).toBe(true)
    expect(line.split(', ')).not.toContain('193.0.0.0/8')
    // /8 -> 24 префикса, вместо одного
    expect(line.split(', ').length).toBe(base.split(', ').length + 23)
    // остальные префиксы не тронуты
    expect(line).toContain('1.0.0.0/8')
  })

  it('endpoint на границах диапазона: первый и последний адрес публичного блока', () => {
    for (const host of ['193.0.0.0', '193.255.255.255', '1.0.0.0', '223.255.255.255']) {
      const line = wgAllowedIps(true, [], host)
      expect(covered(line, host), host).toBe(false)
    }
  })

  it('endpoint в /4: вычитание даёт 28 префиксов вместо одного', () => {
    const base = wgAllowedIps(true)
    const line = wgAllowedIps(true, [], '16.0.0.1')
    expect(line.split(', ').length).toBe(base.split(', ').length + 27)
    expect(covered(line, '16.0.0.1')).toBe(false)
    expect(covered(line, '16.0.0.0')).toBe(true)
  })

  it('endpoint в локальной сети (не в списке) - список не меняется', () => {
    expect(wgAllowedIps(true, [], '10.1.2.3')).toBe(wgAllowedIps(true))
  })

  it('endpoint-имя: берётся endpointIp; IP в endpointHost приоритетнее endpointIp', () => {
    const byIp = wgAllowedIps(true, [], '193.5.6.7')
    expect(wgAllowedIps(true, [], 'vpn.example.com', '193.5.6.7')).toBe(byIp)
    expect(wgAllowedIps(true, [], 'vpn.example.com', ' 193.5.6.7 ')).toBe(byIp)
    expect(wgAllowedIps(true, [], '193.5.6.7', '8.8.8.8')).toBe(byIp)
  })

  it('невалидный endpoint (октет >255, мало октетов, пусто, v6) - не ломает и не вычитает', () => {
    const base = wgAllowedIps(true)
    for (const host of ['', '256.1.1.1', '1.2.3', '1.2.3.4.5', 'a.b.c.d', '2001:db8::1']) {
      expect(wgAllowedIps(true, [], host), host).toBe(base)
    }
    expect(wgAllowedIps(true, [], 'vpn.example.com', 'мусор')).toBe(base)
  })

  it('результат детерминирован', () => {
    expect(wgAllowedIps(true, ['10.66.66.'], '193.5.6.7')).toBe(wgAllowedIps(true, ['10.66.66.'], '193.5.6.7'))
  })
})

describe('ovpnLocalRoutes', () => {
  it('без сплита - пусто', () => {
    expect(ovpnLocalRoutes(false)).toEqual([])
    expect(ovpnLocalRoutes(false, ['10.66.66.'])).toEqual([])
  })

  it('сплит без префиксов - комментарий и четыре net_gateway маршрута', () => {
    const lines = ovpnLocalRoutes(true)
    expect(lines).toHaveLength(5)
    expect(lines[0]!.startsWith('#')).toBe(true)
    expect(lines.slice(1)).toEqual([
      'route 10.0.0.0 255.0.0.0 net_gateway',
      'route 100.64.0.0 255.192.0.0 net_gateway',
      'route 172.16.0.0 255.240.0.0 net_gateway',
      'route 192.168.0.0 255.255.0.0 net_gateway',
    ])
  })

  it('префиксы туннеля добавляются через vpn_gateway по порядку; битые пропускаются', () => {
    const lines = ovpnLocalRoutes(true, ['10.66.66.', 'x', '', '10.99.0.'])
    expect(lines.slice(5)).toEqual([
      'route 10.66.66.0 255.255.255.0 vpn_gateway',
      'route 10.99.0.0 255.255.255.0 vpn_gateway',
    ])
  })
})
