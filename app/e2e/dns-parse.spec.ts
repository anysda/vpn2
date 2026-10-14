import { test, expect } from '@playwright/test'
import { serverModule } from './support/server-module'

// Разбор ответов AdGuard Home для вкладки DNS. Записи — в форме, которую
// отдаёт настоящий AdGuard Home 0.107 (/control/querylog, /control/stats).
type Parse = typeof import('../server/utils/agh-parse')
const p = serverModule<Parse>(new URL('../server/utils/agh-parse.ts', import.meta.url), {})

const lists = new Map([[1, 'AdGuard DNS filter']])
const services = new Map([['youtube', 'YouTube']])
const names = new Map([['10.66.66.2', 'Ноутбук Пети']])

const entry = (over: Record<string, unknown>) => ({
  time: '2026-10-14T10:00:00.000000000+03:00',
  question: { class: 'IN', name: 'example.org', type: 'A' },
  client: '10.66.66.2',
  client_info: { whois: {}, name: '', disallowed_rule: '', disallowed: false },
  reason: 'NotFilteredNotFound',
  rules: [],
  service_name: null,
  answer: [{ type: 'A', value: '203.0.113.7', ttl: 300 }],
  cached: false,
  upstream: 'https://1.1.1.1:443/dns-query',
  elapsedMs: '24.31',
  ...over,
}) as never

test('журнал: состояние строки и подпись по причине AdGuard', () => {
  const c = (over: Record<string, unknown>) => {
    const { kind, detail } = p.logRow(entry(over), names, lists, services)
    return { kind, detail }
  }
  expect(c({})).toEqual({ kind: 'ok', detail: '1.1.1.1 · 24 мс' })
  expect(c({ elapsedMs: '0.12270299999999999', cached: true })).toEqual({ kind: 'ok', detail: 'из кэша' })
  expect(c({ upstream: '', elapsedMs: '3.46' })).toEqual({ kind: 'ok', detail: '3,5 мс' })
  expect(c({ reason: 'FilteredBlackList', rules: [{ filter_list_id: 0, text: '||example.org^' }] }))
    .toEqual({ kind: 'blk', detail: 'Свои правила' })
  expect(c({ reason: 'FilteredBlackList', rules: [{ filter_list_id: 1, text: '||example.org^' }] }))
    .toEqual({ kind: 'blk', detail: 'AdGuard DNS filter' })
  expect(c({ reason: 'FilteredBlackList', rules: [{ filter_list_id: 7, text: '||example.org^' }] }))
    .toEqual({ kind: 'blk', detail: 'Список 7' })
  expect(c({ reason: 'FilteredBlockedService', service_name: 'youtube', rules: [{ filter_list_id: -2, text: '||youtube.com^' }] }))
    .toEqual({ kind: 'blk', detail: 'Сервис: YouTube' })
  expect(c({ reason: 'Rewrite', answer: [{ type: 'A', value: '192.0.2.1', ttl: 10 }] }))
    .toEqual({ kind: 'rw', detail: '→ 192.0.2.1' })
  expect(c({ reason: 'NotFilteredWhiteList', rules: [{ filter_list_id: 0, text: '@@||example.org^' }] }))
    .toEqual({ kind: 'ok', detail: 'Свои правила' })
  expect(c({ reason: 'FilteredSafeBrowsing' })).toEqual({ kind: 'safe', detail: 'Безопасная навигация' })
})

test('журнал: клиент по имени из панели, иначе по имени в AdGuard, иначе адрес', () => {
  const row = (over: Record<string, unknown>) => p.logRow(entry(over), names, lists, services)
  expect(row({})).toMatchObject({ client: 'Ноутбук Пети', ip: '10.66.66.2', domain: 'example.org', type: 'A' })
  expect(row({ client: '10.66.66.9', client_info: { name: 'Пётр' } }).client).toBe('Пётр')
  expect(row({ client: '10.66.66.9', client_info: null }).client).toBe('10.66.66.9')
})

test('журнал: фильтр и поиск по домену, имени и адресу клиента', () => {
  const r = (kind: string) => ({ time: '', domain: 'ads.Example.net', type: 'A', kind, detail: '', client: 'Ноутбук Пети', ip: '10.66.66.2' }) as never
  expect(p.logMatch(r('blk'), 'blk', '')).toBe(true)
  expect(p.logMatch(r('safe'), 'blk', '')).toBe(true)
  expect(p.logMatch(r('ok'), 'blk', '')).toBe(false)
  expect(p.logMatch(r('rw'), 'rw', '')).toBe(true)
  expect(p.logMatch(r('rw'), 'ok', '')).toBe(false)
  expect(p.logMatch(r('ok'), 'all', 'example')).toBe(true)
  expect(p.logMatch(r('ok'), 'all', 'пети')).toBe(true)
  expect(p.logMatch(r('ok'), 'all', '10.66.66')).toBe(true)
  expect(p.logMatch(r('ok'), 'all', 'github')).toBe(false)
})

test('разблокировка из журнала снимает своё правило блокировки и не трогает чужие', () => {
  expect(p.toggleRules(['||mc.yandex.ru^', '# заметка', '||ads.net^'], 'mc.yandex.ru', false))
    .toEqual({ rules: ['# заметка', '||ads.net^'], removed: true, rule: '@@||mc.yandex.ru^' })
  expect(p.toggleRules(['@@||t.co^'], 'mc.yandex.ru', false))
    .toEqual({ rules: ['@@||t.co^'], removed: false, rule: '@@||mc.yandex.ru^' })
  expect(p.toggleRules(['@@||t.co^', '||x.org^'], 't.co', true))
    .toEqual({ rules: ['||x.org^'], removed: true, rule: '||t.co^' })
})

test('сводка: цифры совпадают с /control/stats', () => {
  const hours = (f: (i: number) => number) => Array.from({ length: 168 }, (_, i) => f(i))
  const stats = {
    time_units: 'hours' as const,
    num_dns_queries: 2400,
    num_blocked_filtering: 240,
    num_replaced_safebrowsing: 3,
    num_replaced_parental: 1,
    avg_processing_time: 0.0142,
    dns_queries: hours(i => i < 144 ? 7 : 100),
    blocked_filtering: hours(i => i < 144 ? 1 : 10),
    replaced_safebrowsing: hours(i => i === 160 ? 3 : 0),
    replaced_parental: hours(i => i === 150 ? 1 : 0),
    top_queried_domains: [{ 'github.com': 120 }, { 'chatgpt.com': 80 }],
    top_clients: [{ '10.66.66.2': 300 }, { '10.66.66.3': 100 }],
    top_blocked_domains: [{ 'mc.yandex.ru': 31 }],
    top_upstreams_responses: [{ 'https://1.1.1.1:443/dns-query': 5 }, { '9.9.9.9:53': 3 }, { 'tls://dns.google:853': 1 }],
    top_upstreams_avg_time: [{ 'https://1.1.1.1:443/dns-query': 0.02 }, { '9.9.9.9:53': 0.0071 }],
  }
  const recent = [
    entry({ cached: true }), entry({}), entry({ elapsedMs: '10' }), entry({ reason: 'FilteredBlackList', upstream: '' }),
  ]
  const s = p.summarize(stats, names, 'parallel', recent)
  expect(s).toMatchObject({
    units: 'hours', queries: 2400, blocked: 240, blockedPct: 10, threats: 4, avgMs: expect.closeTo(14.2, 6),
    cachedPct: 33, mode: 'параллельно',
    clients: [['Ноутбук Пети', 300], ['10.66.66.3', 100]],
    domains: [['github.com', 120], ['chatgpt.com', 80]],
    blocks: [['mc.yandex.ru', 31]],
  })
  expect(s.q).toHaveLength(24)
  expect(s.upstreams).toEqual([
    { url: 'https://1.1.1.1:443/dns-query', pct: 56, ms: expect.closeTo(20, 6) },
    { url: '9.9.9.9:53', pct: 33, ms: expect.closeTo(7.1, 6) },
    { url: 'tls://dns.google:853', pct: 11, ms: 0 },
  ])
  expect(p.summarize({ ...stats, time_units: 'days', dns_queries: [5, 6, 7] }, names, '', []))
    .toMatchObject({ units: 'days', queries: 18, q: [5, 6, 7], cachedPct: null, mode: 'балансировка' })
})
