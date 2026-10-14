import { test, expect } from '@playwright/test'
import { serverModule } from './support/server-module'

// Настройки, перезаписи и клиенты AdGuard Home для вкладки DNS: проверка ввода
// до отправки и перевод ответов /control/* в то, что показывает панель.
type Settings = typeof import('../server/utils/dns-settings')
const s = serverModule<Settings>(new URL('../server/utils/dns-settings.ts', import.meta.url), {})

const info = {
  upstream_dns: ['https://dns.example/dns-query', '192.0.2.53'],
  bootstrap_dns: ['192.0.2.53', '192.0.2.54'],
  upstream_mode: '',
  dnssec_enabled: true,
  disable_ipv6: true,
  cache_size: 4194304,
  cache_optimistic: false,
  ratelimit: 0,
  blocking_mode: 'custom_ip',
  blocking_ipv4: '192.0.2.9',
  blocking_ipv6: '::',
}

test('перезапись: домен и ответ проверяются, AdGuard дубль бы принял', () => {
  expect(s.checkRewrite('nas.home', '192.0.2.10')).toBeNull()
  expect(s.checkRewrite('*.lan', '2001:db8::1')).toBeNull()
  expect(s.checkRewrite('cdn.example', 'edge.example')).toBeNull()
  expect(s.checkRewrite('a.example', 'A')).toBeNull()
  expect(s.checkRewrite('bad domain', '192.0.2.1')).toBe('«bad domain» — не домен')
  expect(s.checkRewrite('-x-', '192.0.2.1')).toBe('«-x-» — не домен')
  expect(s.checkRewrite('nas.home', '*.example')).toBe('Ответ «*.example» — не IP-адрес и не домен')
  expect(s.sameRewrite({ domain: 'a', answer: 'b' }, { domain: 'a', answer: 'b' })).toBe(true)
  expect(s.sameRewrite({ domain: 'a', answer: 'b' }, { domain: 'a', answer: 'c' })).toBe(false)
})

// Поле настроек так, как его видит вкладка: через settingsView.
const field = (over: Record<string, unknown>) => s.settingsView({ ...info, ...over } as never, false, false, { enabled: false },
  { enabled: true, interval: 0, anonymize_client_ip: false, ignored: [] }, { enabled: true, interval: 0, ignored: [] })

test('ответ на блокировку: значение поля → режим AdGuard', () => {
  expect(field({ blocking_mode: 'default', blocking_ipv4: '' }).block).toBe('0.0.0.0')
  expect(field({ blocking_mode: 'nxdomain', blocking_ipv4: '' }).block).toBe('NXDOMAIN')
  expect(field({}).block).toBe('192.0.2.9')
  expect(s.parseBlock('192.0.2.9', info)).toEqual({ set: null })
  expect(s.parseBlock('nxdomain', info)).toEqual({ set: { blocking_mode: 'nxdomain' } })
  expect(s.parseBlock('REFUSED', info)).toEqual({ set: { blocking_mode: 'refused' } })
  expect(s.parseBlock('0.0.0.0', info)).toEqual({ set: { blocking_mode: 'null_ip' } })
  expect(s.parseBlock('192.0.2.7', { ...info, blocking_ipv6: '' }))
    .toEqual({ set: { blocking_mode: 'custom_ip', blocking_ipv4: '192.0.2.7', blocking_ipv6: '::' } })
  expect(s.parseBlock('2001:db8::1', info)).toEqual({ error: 'Ответ на блокировку: IPv4-адрес, NXDOMAIN или REFUSED' })
})

test('лимит запросов и bootstrap: текст поля → значение или ошибка', () => {
  expect(field({ ratelimit: 20 }).ratelimit).toBe('20 в секунду с клиента')
  expect(field({ ratelimit: 0 }).ratelimit).toBe('без лимита')
  expect(s.parseRatelimit('20 в секунду с клиента')).toBe(20)
  expect(s.parseRatelimit(' 35 ')).toBe(35)
  expect(s.parseRatelimit('Без лимита')).toBe(0)
  expect(s.parseRatelimit('много')).toBeNull()
  expect(s.parseRatelimit('-1')).toBeNull()
  expect(s.parseBootstrap('192.0.2.53, 192.0.2.54  [2001:db8::53]:53')).toEqual({ list: ['192.0.2.53', '192.0.2.54', '[2001:db8::53]:53'] })
  expect(s.parseBootstrap('  ')).toEqual({ error: 'Bootstrap: нужен хотя бы один IP-адрес' })
  expect(s.parseBootstrap('192.0.2.53, dns.google')).toEqual({ error: 'Bootstrap: «dns.google» — не IP-адрес' })
})

test('серверы выше: строки и отказ AdGuard по-русски с номером строки', () => {
  expect(s.upstreamLines(' a \n\n b\n')).toEqual(['a', 'b'])
  expect(s.upstreamError('validating dns config: upstream servers: parsing error at index 1: cannot prepare the upstream: invalid address bad host!!'))
    .toBe('Строка 2: AdGuard не понял адрес сервера')
  expect(s.upstreamError('something else')).toBe('AdGuard не принял серверы выше')
})

test('вид настроек: пустой режим — балансировка, кэш в МБ, интервалы в часах', () => {
  expect(field({ cache_size: 4194304 }).cache).toBe('4 МБ')
  expect(field({ cache_size: 1572864 }).cache).toBe('1,5 МБ')
  expect(field({ cache_size: 0 }).cache).toBe('выключен')
  expect(field({ cache_size: 4194304, cache_enabled: false }).cache).toBe('выключен')
  const v = s.settingsView(info, true, false, { enabled: true },
    { enabled: true, interval: 7_776_000_000, anonymize_client_ip: true, ignored: [] },
    { enabled: true, interval: 86_400_000, ignored: [] })
  expect(v).toEqual({
    upstreams: 'https://dns.example/dns-query\n192.0.2.53',
    mode: 'load_balance',
    bootstrap: '192.0.2.53, 192.0.2.54',
    safebrowsing: true,
    parental: false,
    safesearch: true,
    dnssec: true,
    noAaaa: true,
    cache: '4 МБ',
    optimistic: false,
    ratelimit: 'без лимита',
    block: '192.0.2.9',
    logHours: 2160,
    statsHours: 24,
    anonymize: true,
  })
  expect(s.settingsView({ ...info, upstream_dns: null, bootstrap_dns: null, upstream_mode: 'parallel' }, false, false, { enabled: false },
    { enabled: true, interval: 0, anonymize_client_ip: false, ignored: [] }, { enabled: true, interval: 0, ignored: [] }))
    .toMatchObject({ upstreams: '', bootstrap: '', mode: 'parallel' })
})

test('клиенты: свой клиент AdGuard ищется по адресу или имени, иначе общие настройки', () => {
  const agh = [
    { name: 'другое имя', ids: ['10.66.66.2'], use_global_settings: false, filtering_enabled: true, safe_search: { enabled: true }, use_global_blocked_services: false, blocked_services: ['tiktok', 'roblox'] },
    { name: 'Настя', ids: ['192.0.2.100'], use_global_settings: false, filtering_enabled: false, safesearch_enabled: false, use_global_blocked_services: false, blocked_services: null },
  ]
  const panel = [
    { name: 'Мама', tg: 'mama', filter: true, ips: ['10.66.66.2', '10.66.66.3'] },
    { name: 'Настя', tg: null, filter: true, ips: [] },
    { name: 'Папа', tg: '@papa', filter: false, ips: ['10.66.66.4'] },
  ]
  const rows = s.clientRows(panel, agh, new Map([['10.66.66.2', 5], ['10.66.66.3', 7]]), new Map([['tiktok', 'TikTok']]))
  expect(rows).toEqual([
    { name: 'Мама', tg: '@mama', ips: ['10.66.66.2', '10.66.66.3'], filters: 'common', safesearch: 'on', services: 'TikTok, roblox', queries: 12 },
    { name: 'Настя', tg: 'без Telegram', ips: [], filters: 'off', safesearch: 'off', services: 'ничего', queries: 0 },
    { name: 'Папа', tg: '@papa', ips: ['10.66.66.4'], filters: 'off', safesearch: 'global', services: null, queries: 0 },
  ])
})
