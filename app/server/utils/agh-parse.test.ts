import { describe, expect, it } from 'vitest'
import { logMatch, logRow, summarize, toggleRules } from './agh-parse'
import type { AghLogEntry, AghStats, LogRow } from './agh-parse'

const entry = (over: Partial<AghLogEntry> = {}): AghLogEntry => ({
  client: '10.99.0.5', question: { name: 'example.com', type: 'A' }, reason: 'NotFilteredNotFound', time: '2026-01-01T00:00:00Z', ...over,
})
const none = new Map<string, string>()

describe('logRow', () => {
  const row = (e: AghLogEntry, names = none, lists = new Map<number, string>(), services = none) => logRow(e, names, lists, services)

  it('обычный ответ: апстрим без схемы и порта и время', () => {
    const r = row(entry({ upstream: 'tls://dns.google:853', elapsedMs: '12.4' }))
    expect(r).toEqual({ time: '2026-01-01T00:00:00Z', domain: 'example.com', type: 'A', kind: 'ok', detail: 'dns.google · 12 мс', client: '10.99.0.5', ip: '10.99.0.5' })
  })

  it('время: меньше 10 - один знак с запятой, 10 и больше - целое, отсутствует - 0', () => {
    expect(row(entry({ upstream: '1.1.1.1', elapsedMs: '9.96' })).detail).toBe('1.1.1.1 · 10 мс')
    expect(row(entry({ upstream: '1.1.1.1', elapsedMs: '3.14' })).detail).toBe('1.1.1.1 · 3,1 мс')
    expect(row(entry({ upstream: '1.1.1.1', elapsedMs: '9.5' })).detail).toBe('1.1.1.1 · 9,5 мс')
    expect(row(entry({ upstream: '1.1.1.1', elapsedMs: '10.4' })).detail).toBe('1.1.1.1 · 10 мс')
    expect(row(entry({ upstream: '1.1.1.1' })).detail).toBe('1.1.1.1 · 0 мс')
    expect(row(entry({ elapsedMs: '2' })).detail).toBe('2 мс')
  })

  it('хост апстрима: https с путём, IPv6 в скобках, голый адрес', () => {
    expect(row(entry({ upstream: 'https://dns.google/dns-query' })).detail).toContain('dns.google ·')
    expect(row(entry({ upstream: 'tls://[2001:db8::1]:853' })).detail).toContain('[2001:db8::1] ·')
    expect(row(entry({ upstream: '8.8.8.8:53' })).detail).toContain('8.8.8.8 ·')
  })

  it('кэш: без апстрима и времени', () => {
    expect(row(entry({ cached: true, upstream: 'x', elapsedMs: '5' }))).toMatchObject({ kind: 'ok', detail: 'из кэша' })
  })

  it('блокировка по списку: свои правила (id 0 / нет правил), список из таблицы, неизвестный список', () => {
    const lists = new Map([[7, 'AdGuard DNS filter']])
    expect(row(entry({ reason: 'FilteredBlackList', rules: [{ text: '||x^', filter_list_id: 0 }] }), none, lists)).toMatchObject({ kind: 'blk', detail: 'Свои правила' })
    expect(row(entry({ reason: 'FilteredBlackList' }), none, lists).detail).toBe('Свои правила')
    expect(row(entry({ reason: 'FilteredBlackList', rules: [] }), none, lists).detail).toBe('Свои правила')
    expect(row(entry({ reason: 'FilteredBlackList', rules: [{ text: 'x', filter_list_id: 7 }] }), none, lists).detail).toBe('AdGuard DNS filter')
    expect(row(entry({ reason: 'FilteredInvalid', rules: [{ text: 'x', filter_list_id: 9 }] }), none, lists).detail).toBe('Список 9')
    expect(row(entry({ reason: 'FilteredInvalid', rules: null })).kind).toBe('blk')
  })

  it('заблокированный сервис: имя из таблицы, иначе id, без поля - пусто', () => {
    const services = new Map([['youtube', 'YouTube']])
    expect(row(entry({ reason: 'FilteredBlockedService', service_name: 'youtube' }), none, undefined, services)).toMatchObject({ kind: 'blk', detail: 'Сервис: YouTube' })
    expect(row(entry({ reason: 'FilteredBlockedService', service_name: 'tiktok' })).detail).toBe('Сервис: tiktok')
    expect(row(entry({ reason: 'FilteredBlockedService' })).detail).toBe('Сервис: ')
  })

  it('safe: безопасная навигация и родительский контроль', () => {
    expect(row(entry({ reason: 'FilteredSafeBrowsing' }))).toMatchObject({ kind: 'safe', detail: 'Безопасная навигация' })
    expect(row(entry({ reason: 'FilteredParental' }))).toMatchObject({ kind: 'safe', detail: 'Родительский контроль' })
  })

  it('переписывание: первый непустой ответ, нет ответов - «пусто»', () => {
    for (const reason of ['Rewrite', 'RewriteEtcHosts', 'RewriteRule', 'FilteredSafeSearch']) {
      expect(row(entry({ reason, answer: [{ type: 'A', value: '' }, { type: 'A', value: '1.2.3.4' }, { type: 'A', value: '5.6.7.8' }] })), reason).toMatchObject({ kind: 'rw', detail: '→ 1.2.3.4' })
    }
    expect(row(entry({ reason: 'Rewrite', answer: null })).detail).toBe('→ пусто')
    expect(row(entry({ reason: 'Rewrite' })).detail).toBe('→ пусто')
    expect(row(entry({ reason: 'Rewrite', answer: [] })).detail).toBe('→ пусто')
  })

  it('белый список: источник правила', () => {
    expect(row(entry({ reason: 'NotFilteredWhiteList', rules: [{ text: '@@||a^', filter_list_id: 0 }] }))).toMatchObject({ kind: 'ok', detail: 'Свои правила' })
  })

  it('незнакомая причина разбирается как обычный ответ', () => {
    expect(row(entry({ reason: 'SomethingNew', elapsedMs: '1' }))).toMatchObject({ kind: 'ok', detail: '1 мс' })
  })

  it('имя клиента: из панели, затем из AdGuard, затем адрес', () => {
    expect(row(entry({ client_info: { name: 'agh' } }), new Map([['10.99.0.5', 'Телефон']])).client).toBe('Телефон')
    expect(row(entry({ client_info: { name: 'agh' } })).client).toBe('agh')
    expect(row(entry({ client_info: {} })).client).toBe('10.99.0.5')
    expect(row(entry({ client_info: null })).client).toBe('10.99.0.5')
    expect(row(entry()).ip).toBe('10.99.0.5')
  })
})

describe('logMatch', () => {
  const r = (kind: LogRow['kind'], over: Partial<LogRow> = {}): LogRow => ({ time: 't', domain: 'Example.COM', type: 'A', kind, detail: '', client: 'Телефон Анны', ip: '10.99.0.5', ...over })

  it('фильтры: all, blk (включая safe), ok, rw', () => {
    const kinds: LogRow['kind'][] = ['ok', 'blk', 'rw', 'safe']
    const pass = (f: 'all' | 'blk' | 'ok' | 'rw') => kinds.filter(k => logMatch(r(k), f, ''))
    expect(pass('all')).toEqual(['ok', 'blk', 'rw', 'safe'])
    expect(pass('blk')).toEqual(['blk', 'safe'])
    expect(pass('ok')).toEqual(['ok'])
    expect(pass('rw')).toEqual(['rw'])
  })

  it('поиск: домен и клиент без учёта регистра (в том числе кириллица), ip по подстроке', () => {
    expect(logMatch(r('ok'), 'all', 'example')).toBe(true)
    expect(logMatch(r('ok'), 'all', 'EXAMPLE.com')).toBe(true)
    expect(logMatch(r('ok'), 'all', 'ТЕЛЕФОН')).toBe(true)
    expect(logMatch(r('ok'), 'all', '99.0')).toBe(true)
    expect(logMatch(r('ok'), 'all', 'nothing')).toBe(false)
  })

  it('ip-поиск регистр не приводит; поиск работает вместе с фильтром', () => {
    expect(logMatch(r('ok', { ip: 'FE80::1' }), 'all', 'fe80')).toBe(false)
    expect(logMatch(r('ok'), 'blk', 'example')).toBe(false)
    expect(logMatch(r('blk'), 'blk', 'example')).toBe(true)
  })

  it('пустой запрос - совпадает', () => {
    expect(logMatch(r('ok'), 'all', '')).toBe(true)
  })
})

describe('toggleRules', () => {
  it('блокировка: убирает точное разрешающее правило и возвращает запрещающее', () => {
    expect(toggleRules(['a', '@@||x.com^', 'b'], 'x.com', true)).toEqual({ rules: ['a', 'b'], removed: true, rule: '||x.com^' })
  })

  it('разблокировка: убирает точное запрещающее правило, возвращает разрешающее', () => {
    expect(toggleRules(['||x.com^', 'a'], 'x.com', false)).toEqual({ rules: ['a'], removed: true, rule: '@@||x.com^' })
  })

  it('нет противоположного правила - список прежний, removed=false', () => {
    expect(toggleRules(['||x.com^'], 'x.com', true)).toEqual({ rules: ['||x.com^'], removed: false, rule: '||x.com^' })
    expect(toggleRules([], 'x.com', true)).toEqual({ rules: [], removed: false, rule: '||x.com^' })
  })

  it('чужие домены и поддомены не трогаются; пробелы по краям игнорируются; дубли убираются все', () => {
    expect(toggleRules(['@@||sub.x.com^', '@@||x.com^.', '  @@||x.com^  ', '@@||x.com^', '@@||ax.com^'], 'x.com', true).rules)
      .toEqual(['@@||sub.x.com^', '@@||x.com^.', '@@||ax.com^'])
  })

  it('исходный массив не меняется', () => {
    const src = ['@@||x.com^']
    toggleRules(src, 'x.com', true)
    expect(src).toEqual(['@@||x.com^'])
  })
})

describe('summarize', () => {
  const stats = (over: Partial<AghStats> = {}): AghStats => ({
    time_units: 'days', num_dns_queries: 0, num_blocked_filtering: 0, num_replaced_safebrowsing: 0, num_replaced_parental: 0, avg_processing_time: 0,
    dns_queries: [], blocked_filtering: [], replaced_safebrowsing: [], replaced_parental: [],
    top_queried_domains: [], top_clients: [], top_blocked_domains: [], top_upstreams_responses: [], top_upstreams_avg_time: [], ...over,
  })

  it('пустая статистика и пустой журнал: нули, cachedPct=null, режим пустой = балансировка', () => {
    expect(summarize(stats(), none, '', [])).toEqual({
      units: 'days', queries: 0, blocked: 0, blockedPct: 0, threats: 0, avgMs: 0, cachedPct: null,
      q: [], b: [], t: [], ms: [], upstreams: [], mode: 'балансировка', clients: [], domains: [], blocks: [],
    })
  })

  it('почасовая: только последние 24 значения; подневная: весь ряд', () => {
    const series = Array.from({ length: 30 }, (_, i) => i + 1)
    const h = summarize(stats({ time_units: 'hours', dns_queries: series }), none, '', [])
    expect(h.q).toHaveLength(24)
    expect(h.q[0]).toBe(7)
    expect(h.queries).toBe(series.slice(6).reduce((a, b) => a + b, 0))
    expect(summarize(stats({ dns_queries: series }), none, '', []).q).toHaveLength(30)
    expect(summarize(stats({ time_units: 'hours', dns_queries: [1, 2, 3] }), none, '', []).q).toEqual([1, 2, 3])
  })

  it('доля блокировок и угрозы (родительский + безопасный), отсутствующие ряды = 0', () => {
    const s = summarize(stats({ dns_queries: [50, 50], blocked_filtering: [10, 15], replaced_parental: [1, 2] }), none, '', [])
    expect(s.blocked).toBe(25)
    expect(s.blockedPct).toBe(25)
    expect(s.t).toEqual([1, 2])
    expect(s.threats).toBe(3)
    const s2 = summarize(stats({ dns_queries: [10], replaced_parental: [1], replaced_safebrowsing: [4] }), none, '', [])
    expect(s2.t).toEqual([5])
    expect(s2.threats).toBe(5)
  })

  it('время ответа в миллисекундах из секунд', () => {
    expect(summarize(stats({ avg_processing_time: 0.0123 }), none, '', []).avgMs).toBeCloseTo(12.3, 5)
  })

  it('кэш: доля среди NotFiltered*, округление', () => {
    const recent = [
      entry({ cached: true }), entry({ cached: true }), entry({}), entry({ reason: 'NotFilteredWhiteList' }),
      entry({ reason: 'FilteredBlackList', cached: true }),
    ]
    expect(summarize(stats(), none, '', recent).cachedPct).toBe(50)
    expect(summarize(stats(), none, '', [entry({ cached: true }), entry({}), entry({})]).cachedPct).toBe(33)
    expect(summarize(stats(), none, '', [entry({ reason: 'FilteredBlackList' })]).cachedPct).toBeNull()
  })

  it('ход времени: журнал новые-первыми разворачивается, кэш и без апстрима отбрасываются, ряд сжимается до 12', () => {
    const recent = [
      entry({ upstream: 'u', elapsedMs: '30' }), entry({ upstream: 'u', cached: true, elapsedMs: '999' }),
      entry({ elapsedMs: '999' }), entry({ upstream: 'u', elapsedMs: '10' }),
    ]
    expect(summarize(stats(), none, '', recent).ms).toEqual([10, 30])
    const many = Array.from({ length: 25 }, (_, i) => entry({ upstream: 'u', elapsedMs: String(i) })).reverse()
    const ms = summarize(stats(), none, '', many).ms
    expect(ms).toHaveLength(9)
    expect(ms[0]).toBe(1)
    expect(ms[8]).toBe(24)
    const twelve = Array.from({ length: 12 }, (_, i) => entry({ upstream: 'u', elapsedMs: String(i) }))
    expect(summarize(stats(), none, '', twelve).ms).toHaveLength(12)
  })

  it('апстримы: до 4, доля округлена, время в мс, нет времени = 0, пустой итог = 0%', () => {
    const s = summarize(stats({
      top_upstreams_responses: [{ a: 50 }, { b: 30 }, { c: 10 }, { d: 5 }, { e: 5 }],
      top_upstreams_avg_time: [{ a: 0.02 }, { b: 0.5 }],
    }), none, 'parallel', [])
    expect(s.upstreams).toEqual([
      { url: 'a', pct: 50, ms: 20 }, { url: 'b', pct: 30, ms: 500 }, { url: 'c', pct: 10, ms: 0 }, { url: 'd', pct: 5, ms: 0 },
    ])
    expect(s.mode).toBe('параллельно')
    const z = summarize(stats({ top_upstreams_responses: [{ a: 0 }] }), none, '', [])
    expect(z.upstreams).toEqual([{ url: 'a', pct: 0, ms: 0 }])
  })

  it('режимы: известные, незнакомый остаётся как есть', () => {
    expect(summarize(stats(), none, 'load_balance', []).mode).toBe('балансировка')
    expect(summarize(stats(), none, 'fastest_addr', []).mode).toBe('самый быстрый')
    expect(summarize(stats(), none, 'weird', []).mode).toBe('weird')
  })

  it('клиенты: имена из панели, одинаковые имена суммируются, по убыванию, топ 7', () => {
    const names = new Map([['10.0.0.1', 'Анна'], ['10.0.0.2', 'Анна']])
    const s = summarize(stats({ top_clients: [{ '10.0.0.1': 5 }, { '10.0.0.3': 8 }, { '10.0.0.2': 4 }] }), names, '', [])
    expect(s.clients).toEqual([['Анна', 9], ['10.0.0.3', 8]])
    const eight = Array.from({ length: 9 }, (_, i) => ({ [`h${i}`]: 100 - i }))
    expect(summarize(stats({ top_clients: eight }), none, '', []).clients).toHaveLength(7)
  })

  it('топы доменов: порядок сохраняется, максимум 7, пустой объект даёт пару по умолчанию', () => {
    const doms = Array.from({ length: 9 }, (_, i) => ({ [`d${i}.com`]: 9 - i }))
    const s = summarize(stats({ top_queried_domains: doms, top_blocked_domains: [{ 'x.com': 3 }, {}] }), none, '', [])
    expect(s.domains).toHaveLength(7)
    expect(s.domains[0]).toEqual(['d0.com', 9])
    expect(s.blocks).toEqual([['x.com', 3], ['', 0]])
  })

  it('units и пустые ряды: отсутствующие необязательные ряды не бросают', () => {
    const partial = { ...stats({ time_units: 'hours' }) } as Partial<AghStats>
    delete partial.replaced_parental
    delete partial.replaced_safebrowsing
    expect(() => summarize(partial as AghStats, none, '', [])).not.toThrow()
  })
})
