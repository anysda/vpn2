import { describe, expect, it } from 'vitest'
import { matchRoute } from './route-match'
import type { Conn, RoutePlan } from './route-match'

const conn = (over: Partial<Conn> = {}): Conn => ({ inbound: 'in', network: 'tcp', protocol: 'tls', port: 443, ...over })
const plan = (rules: Array<Record<string, unknown>>, rule_set: RoutePlan['rule_set'] = {}): RoutePlan => ({ rules, final: 'direct', rule_set })

describe('matchRoute: общие', () => {
  it('пустой план и отсутствие совпадений - null (идёт route.final)', () => {
    expect(matchRoute(plan([]), conn())).toBeNull()
    expect(matchRoute(plan([{ domain: ['a.com'], outbound: 'x' }]), conn({ domain: 'b.com' }))).toBeNull()
  })

  it('первое совпавшее правило выигрывает, индекс и outbound возвращаются', () => {
    const p = plan([
      { domain: ['a.com'], outbound: 'one' },
      { domain: ['a.com'], outbound: 'two' },
    ])
    expect(matchRoute(p, conn({ domain: 'a.com' }))).toEqual({
      index: 0, action: 'route', outbound: 'one', by: { kind: 'domain', value: 'a.com', sets: [] },
    })
  })

  it('sniff/resolve/route-options пропускаются, прочие действия итоговые', () => {
    const p = plan([
      { action: 'sniff' },
      { action: 'resolve' },
      { action: 'route-options', domain: ['a.com'] },
      { action: 'reject', domain: ['a.com'] },
    ])
    const hit = matchRoute(p, conn({ domain: 'a.com' }))
    expect(hit?.index).toBe(3)
    expect(hit?.action).toBe('reject')
    expect(hit?.outbound).toBe('')
  })

  it('правило без условий совпадает всегда, by = null', () => {
    expect(matchRoute(plan([{ outbound: 'all' }]), conn())).toEqual({ index: 0, action: 'route', outbound: 'all', by: null })
  })

  it('неизвестное условие не считается совпавшим', () => {
    expect(matchRoute(plan([{ user: ['u'], outbound: 'x' }]), conn())).toBeNull()
  })
})

describe('matchRoute: домены', () => {
  const m = (rule: Record<string, unknown>, c: Partial<Conn>) => matchRoute(plan([{ ...rule, outbound: 'x' }]), conn(c))

  it('domain - только точное совпадение, регистр значим', () => {
    expect(m({ domain: ['a.com'] }, { domain: 'a.com' })).not.toBeNull()
    expect(m({ domain: ['a.com'] }, { domain: 'www.a.com' })).toBeNull()
    expect(m({ domain: ['a.com'] }, { domain: 'A.com' })).toBeNull()
    expect(m({ domain: 'a.com' }, { domain: 'a.com' })).not.toBeNull()
  })

  it('domain_suffix: без точки - сам домен и поддомены, не чужой хвост; с точкой - только поддомены', () => {
    expect(m({ domain_suffix: ['a.com'] }, { domain: 'a.com' })).not.toBeNull()
    expect(m({ domain_suffix: ['a.com'] }, { domain: 'x.a.com' })).not.toBeNull()
    expect(m({ domain_suffix: ['a.com'] }, { domain: 'xa.com' })).toBeNull()
    expect(m({ domain_suffix: ['.a.com'] }, { domain: 'a.com' })).toBeNull()
    expect(m({ domain_suffix: ['.a.com'] }, { domain: 'x.a.com' })).not.toBeNull()
  })

  it('domain_keyword и domain_regex', () => {
    expect(m({ domain_keyword: ['goog'] }, { domain: 'www.google.com' })?.by).toEqual({ kind: 'domain_keyword', value: 'goog', sets: [] })
    expect(m({ domain_keyword: ['goog'] }, { domain: 'yandex.ru' })).toBeNull()
    expect(m({ domain_regex: ['^ads\\d+\\.'] }, { domain: 'ads12.x.com' })).not.toBeNull()
    expect(m({ domain_regex: ['^ads\\d+\\.'] }, { domain: 'ads.x.com' })).toBeNull()
  })

  it('битый regex не бросает и не совпадает', () => {
    expect(m({ domain_regex: ['('] }, { domain: 'a.com' })).toBeNull()
  })

  it('без домена в соединении доменные условия не совпадают; пустое значение тоже', () => {
    expect(m({ domain: ['a.com'] }, {})).toBeNull()
    expect(m({ domain_suffix: ['a.com'] }, {})).toBeNull()
    expect(m({ domain: [] }, { domain: 'a.com' })).toBeNull()
    expect(m({ domain_suffix: ['a.com'] }, { domain: '' })).toBeNull()
  })

  it('внутри группы адреса - ИЛИ: любой из ключей подходит', () => {
    const rule = { domain: ['no.com'], domain_keyword: ['yes'] }
    expect(m(rule, { domain: 'yes.org' })?.by?.kind).toBe('domain_keyword')
  })
})

describe('matchRoute: ip_cidr', () => {
  const m = (cidr: unknown, ip?: string) => matchRoute(plan([{ ip_cidr: cidr, outbound: 'x' }]), conn({ ip }))

  it('границы /32, /0, /24 и вложенность', () => {
    expect(m(['1.2.3.4/32'], '1.2.3.4')).not.toBeNull()
    expect(m(['1.2.3.4/32'], '1.2.3.5')).toBeNull()
    expect(m(['0.0.0.0/0'], '8.8.8.8')).not.toBeNull()
    expect(m(['10.0.0.0/24'], '10.0.0.255')).not.toBeNull()
    expect(m(['10.0.0.0/24'], '10.0.1.0')).toBeNull()
    expect(m(['10.0.0.0/8', '10.1.0.0/16'], '10.1.2.3')?.by?.value).toBe('10.0.0.0/8')
  })

  it('адрес без маски - хост; v6 с /128 и /0', () => {
    expect(m(['1.2.3.4'], '1.2.3.4')).not.toBeNull()
    expect(m(['1.2.3.4'], '1.2.3.5')).toBeNull()
    expect(m(['2001:db8::1/128'], '2001:db8::1')).not.toBeNull()
    expect(m(['2001:db8::1/128'], '2001:db8::2')).toBeNull()
    expect(m(['::/0'], '2001:db8::5')).not.toBeNull()
    expect(m(['2001:db8::/32'], '2001:db9::1')).toBeNull()
  })

  it('семейства не смешиваются', () => {
    expect(m(['0.0.0.0/0'], '2001:db8::1')).toBeNull()
  })

  it('мусор в списке пропускается, без ip соединения - null', () => {
    expect(m(['not-an-ip', '1.2.3.4/32'], '1.2.3.4')).not.toBeNull()
    expect(m(['not-an-ip'], '1.2.3.4')).toBeNull()
    expect(m(['1.2.3.4/32'], undefined)).toBeNull()
    expect(m([], '1.2.3.4')).toBeNull()
  })

  it('в больших наборах (>64) подписью остаётся адрес, в малых - подсеть', () => {
    const big = Array.from({ length: 70 }, (_, i) => `20.${i}.0.0/16`)
    expect(m(big, '20.5.1.1')?.by?.value).toBe('20.5.1.1')
    expect(m(big.slice(0, 64), '20.5.1.1')?.by?.value).toBe('20.5.0.0/16')
  })
})

describe('matchRoute: ip_is_private и ip_version', () => {
  const m = (rule: Record<string, unknown>, ip?: string) => matchRoute(plan([{ ...rule, outbound: 'x' }]), conn({ ip }))

  it('границы приватных диапазонов', () => {
    for (const ip of ['10.0.0.1', '172.16.0.1', '172.31.255.255', '192.168.1.1', '100.64.0.1', '127.0.0.1', '169.254.1.1', 'fe80::1', 'fd00::1', '::1']) {
      expect(m({ ip_is_private: true }, ip), ip).not.toBeNull()
    }
    for (const ip of ['172.15.255.255', '172.32.0.0', '100.63.255.255', '100.128.0.0', '8.8.8.8', '2001:db8::1']) {
      expect(m({ ip_is_private: true }, ip), ip).toBeNull()
    }
  })

  it('ip_is_private=false не совпадает, без ip не совпадает', () => {
    expect(m({ ip_is_private: false }, '10.0.0.1')).toBeNull()
    expect(m({ ip_is_private: true }, undefined)).toBeNull()
  })

  it('ip_version 4 и 6', () => {
    expect(m({ ip_version: 4 }, '1.2.3.4')).not.toBeNull()
    expect(m({ ip_version: 4 }, '2001:db8::1')).toBeNull()
    expect(m({ ip_version: 6 }, '2001:db8::1')).not.toBeNull()
    expect(m({ ip_version: 6 }, undefined)).toBeNull()
  })
})

describe('matchRoute: порты, network, protocol, inbound', () => {
  const m = (rule: Record<string, unknown>, c: Partial<Conn> = {}) => matchRoute(plan([{ ...rule, outbound: 'x' }]), conn(c))

  it('port - точное, в том числе строкой и скаляром', () => {
    expect(m({ port: [443] })).not.toBeNull()
    expect(m({ port: ['443'] })).not.toBeNull()
    expect(m({ port: 443 })).not.toBeNull()
    expect(m({ port: [80] })).toBeNull()
  })

  it('port_range: границы включены, открытые концы', () => {
    expect(m({ port_range: ['400:500'] }, { port: 400 })).not.toBeNull()
    expect(m({ port_range: ['400:500'] }, { port: 500 })).not.toBeNull()
    expect(m({ port_range: ['400:500'] }, { port: 399 })).toBeNull()
    expect(m({ port_range: ['400:500'] }, { port: 501 })).toBeNull()
    expect(m({ port_range: ['400:'] }, { port: 65535 })).not.toBeNull()
    expect(m({ port_range: [':500'] }, { port: 1 })).not.toBeNull()
    expect(m({ port_range: [':500'] }, { port: 501 })).toBeNull()
  })

  it('port и port_range - ИЛИ между собой', () => {
    expect(m({ port: [80], port_range: ['400:500'] }, { port: 443 })).not.toBeNull()
    expect(m({ port: [80], port_range: ['1:2'] }, { port: 443 })).toBeNull()
  })

  it('условия источника-порта панель не проверяет - правило не совпадает', () => {
    expect(m({ source_port: [1234] })).toBeNull()
    expect(m({ source_port_range: ['1:65535'] })).toBeNull()
  })

  it('network, protocol, inbound - по списку, регистр значим', () => {
    expect(m({ network: 'tcp' })).not.toBeNull()
    expect(m({ network: ['udp'] })).toBeNull()
    expect(m({ protocol: ['tls', 'http'] })).not.toBeNull()
    expect(m({ protocol: ['TLS'] })).toBeNull()
    expect(m({ inbound: ['in'] })).not.toBeNull()
    expect(m({ inbound: ['other'] })).toBeNull()
  })

  it('группы объединяются через И', () => {
    const rule = { domain: ['a.com'], port: [443], network: ['tcp'] }
    expect(m(rule, { domain: 'a.com' })).not.toBeNull()
    expect(m(rule, { domain: 'a.com', port: 80 })).toBeNull()
    expect(m(rule, { domain: 'a.com', network: 'udp' })).toBeNull()
    expect(m(rule, { domain: 'b.com' })).toBeNull()
  })
})

describe('matchRoute: источник клиента', () => {
  const m = (rule: Record<string, unknown>, source?: string) => matchRoute(plan([{ ...rule, outbound: 'x' }]), conn({ source }))

  it('source_ip_cidr: совпадение, граница, без source - null', () => {
    expect(m({ source_ip_cidr: ['10.99.0.0/24'] }, '10.99.0.7')?.by).toEqual({ kind: 'source_ip_cidr', value: '10.99.0.7', sets: [] })
    expect(m({ source_ip_cidr: ['10.99.0.0/24'] }, '10.99.1.7')).toBeNull()
    expect(m({ source_ip_cidr: ['10.99.0.0/24'] }, undefined)).toBeNull()
  })

  it('source_ip_is_private', () => {
    expect(m({ source_ip_is_private: true }, '192.168.0.2')).not.toBeNull()
    expect(m({ source_ip_is_private: true }, '8.8.8.8')).toBeNull()
  })

  it('источник И адрес назначения: by берётся из адреса назначения', () => {
    const hit = matchRoute(plan([{ domain: ['a.com'], source_ip_cidr: ['10.0.0.0/8'], outbound: 'x' }]), conn({ domain: 'a.com', source: '10.1.1.1' }))
    expect(hit?.by?.kind).toBe('domain')
    expect(matchRoute(plan([{ domain: ['a.com'], source_ip_cidr: ['10.0.0.0/8'], outbound: 'x' }]), conn({ domain: 'a.com', source: '11.1.1.1' }))).toBeNull()
  })
})

describe('matchRoute: rule_set, logical, invert', () => {
  it('rule_set: подпись несёт тег набора; неизвестный тег не совпадает', () => {
    const p = plan([{ rule_set: ['ru', 'ads'], outbound: 'x' }], { ads: [{ domain_suffix: ['ad.com'] }] })
    expect(matchRoute(p, conn({ domain: 'x.ad.com' }))?.by).toEqual({ kind: 'domain_suffix', value: 'ad.com', sets: ['ads'] })
    expect(matchRoute(p, conn({ domain: 'good.com' }))).toBeNull()
  })

  it('rule_set вложен в rule_set - цепочка тегов', () => {
    const p = plan([{ rule_set: ['outer'], outbound: 'x' }], {
      outer: [{ rule_set: ['inner'] }],
      inner: [{ ip_cidr: ['1.1.1.0/24'] }],
    })
    expect(matchRoute(p, conn({ ip: '1.1.1.1' }))?.by).toEqual({ kind: 'ip_cidr', value: '1.1.1.0/24', sets: ['outer', 'inner'] })
  })

  it('logical and: все подправила; or: любое; пустой and не совпадает', () => {
    const and = { type: 'logical', mode: 'and', rules: [{ domain: ['a.com'] }, { port: [443] }], outbound: 'x' }
    expect(matchRoute(plan([and]), conn({ domain: 'a.com' }))).not.toBeNull()
    expect(matchRoute(plan([and]), conn({ domain: 'a.com', port: 80 }))).toBeNull()
    const or = { type: 'logical', mode: 'or', rules: [{ domain: ['a.com'] }, { port: [80] }], outbound: 'x' }
    expect(matchRoute(plan([or]), conn({ domain: 'b.com', port: 80 }))).not.toBeNull()
    expect(matchRoute(plan([or]), conn({ domain: 'b.com' }))).toBeNull()
    expect(matchRoute(plan([{ type: 'logical', mode: 'and', rules: [], outbound: 'x' }]), conn())).toBeNull()
    expect(matchRoute(plan([{ type: 'logical', mode: 'or', rules: [], outbound: 'x' }]), conn())).toBeNull()
  })

  it('logical: подпись берётся из первого совпавшего не-invert подправила', () => {
    const or = { type: 'logical', mode: 'or', rules: [{ domain: ['zzz.com'] }, { domain: ['a.com'] }], outbound: 'x' }
    expect(matchRoute(plan([or]), conn({ domain: 'a.com' }))?.by?.value).toBe('a.com')
  })

  it('invert: на обычном правиле и на logical', () => {
    const inv = { domain: ['a.com'], invert: true, outbound: 'x' }
    expect(matchRoute(plan([inv]), conn({ domain: 'a.com' }))).toBeNull()
    expect(matchRoute(plan([inv]), conn({ domain: 'b.com' }))).toEqual({ index: 0, action: 'route', outbound: 'x', by: null })
    const linv = { type: 'logical', mode: 'and', invert: true, rules: [{ domain: ['a.com'] }], outbound: 'x' }
    expect(matchRoute(plan([linv]), conn({ domain: 'a.com' }))).toBeNull()
    expect(matchRoute(plan([linv]), conn({ domain: 'b.com' }))).not.toBeNull()
  })

  it('action по умолчанию route; outbound по умолчанию пусто', () => {
    expect(matchRoute(plan([{ domain: ['a.com'] }]), conn({ domain: 'a.com' }))).toMatchObject({ action: 'route', outbound: '' })
  })
})
