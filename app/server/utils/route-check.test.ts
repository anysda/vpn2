import { beforeAll, describe, expect, it } from 'vitest'
import type { ClashProxy } from './metrics'
import type { RoutePlan } from './route-match'
import * as rc from './route-check'
import { buildRoutePlan } from '../../scripts/route-plan.mjs'

describe('route-check', () => {
  type Row = { id: number, type: string, value: string, outbound: string }

  const buildPlan = (rows: Row[], sets: Record<string, object[]>, env: Record<string, string> = {}, prefs: Record<string, string[]> = {}): RoutePlan =>
    buildRoutePlan(rows, sets, env, prefs)

  const ROWS: Row[] = [
    { id: 1, type: 'domain', value: 'chatgpt.com', outbound: 'hy2-aa-direct' },
    { id: 2, type: 'domain', value: '*.openai.com', outbound: 'hy2-bb-warp' },
    { id: 3, type: 'ip_cidr', value: '203.0.113.0/24', outbound: 'hy2-bb-direct' },
    { id: 4, type: 'domain', value: 'mail.ru', outbound: 'hy2-aa-direct' },
  ]
  const SETS = {
    'geoip-ru': [{ ip_cidr: ['198.51.100.0/24', '149.154.160.0/20'] }],
    'geosite-category-gov-ru': [{ domain_suffix: ['gosuslugi.example'] }],
    'geosite-youtube': [{ domain_suffix: ['youtube.example'] }],
  }
  const delay = (ms: number) => [{ time: '', delay: ms }]
  const PROXIES: ClashProxy[] = [
    { name: 'hy2-aa-direct', type: 'Trojan', history: delay(40) },
    { name: 'hy2-bb-direct', type: 'Trojan', history: delay(50) },
    { name: 'hy2-bb-warp', type: 'Trojan', history: delay(70) },
    { name: 'direct-ru', type: 'Direct', history: delay(4) },
    { name: 'youtube-ru', type: 'Direct', history: delay(38) },
    { name: 'youtube-auto', type: 'Selector', now: 'youtube-ru' },
    { name: 'foreign-best', type: 'Selector', now: 'hy2-bb-direct' },
    { name: 'pin-hy2-aa-direct', type: 'Selector', now: 'hy2-aa-direct' },
    { name: 'pin-hy2-bb-direct', type: 'Selector', now: 'hy2-bb-direct' },
    { name: 'pin-hy2-bb-warp', type: 'Selector', now: 'hy2-bb-warp' },
    { name: 'lane-01', type: 'Selector', now: 'hy2-bb-direct' },
    { name: 'lane-02', type: 'Selector', now: 'hy2-bb-warp' },
  ]
  // Устройства клиентов из LANE_SUBNETS тестового роутера (192.0.2.0/29, 4 дорожки):
  // .5 - клиент с личным выходом aa (дорожка lane-01), .6 - без него (lane-02).
  const PREFS = { aa: ['192.0.2.5'] }
  const MAMA = '192.0.2.5'
  const OTHER = '192.0.2.6'

  describe('проверка адреса по снимку маршрутов роутера', () => {
    let plan: RoutePlan
    beforeAll(() => {
      plan = buildPlan(ROWS, SETS, { YT_ROUTE: 'zapret' }, PREFS)
    })

    function check(input: string, ip: string | null = null) {
      const q = rc.parseQuery(input)!
      return rc.explain(plan, rc.clientConn(q, ip), ROWS, PROXIES)
    }

    const cases: Array<[string, string, string | null, Partial<ReturnType<typeof rc.explain>>]> = [
      ['точное правило', 'chatgpt.com', null, { reason: 'manual', rule: 'chatgpt.com', routeId: 1, outbound: 'hy2-aa-direct', delay: 40 }],
      ['правило домена берёт и поддомены', 'cdn.chatgpt.com', null, { reason: 'manual', routeId: 1 }],
      ['wildcard', 'api.openai.com', null, { reason: 'manual', rule: '*.openai.com', routeId: 2, outbound: 'hy2-bb-warp', delay: 70 }],
      ['CIDR', '203.0.113.9', null, { reason: 'manual', rule: '203.0.113.0/24', routeId: 3, outbound: 'hy2-bb-direct' }],
      ['ручное правило выше .ru', 'mail.ru', null, { reason: 'manual', routeId: 4, outbound: 'hy2-aa-direct' }],
      ['.рф', 'https://Пример.РФ/путь', null, { reason: 'ru', rule: '.рф', routeId: null, outbound: 'direct-ru', delay: 4 }],
      ['.ru', 'yandex.ru', null, { reason: 'ru', rule: '.ru', outbound: 'direct-ru' }],
      ['gov-ru', 'www.gosuslugi.example', null, { reason: 'ru', rule: 'geosite-category-gov-ru', outbound: 'direct-ru' }],
      ['geoip-ru по IP', '198.51.100.7', null, { reason: 'ru', rule: 'geoip-ru', outbound: 'direct-ru' }],
      ['geoip-ru по адресу домена', 'shop.example', '198.51.100.7', { reason: 'ru', rule: 'geoip-ru', outbound: 'direct-ru' }],
      ['zapret по списку доменов', 'www.youtube.com', null, { reason: 'youtube', rule: 'youtube.com', outbound: 'youtube-ru', delay: 38 }],
      ['zapret по geosite', 'm.youtube.example', null, { reason: 'youtube', rule: 'geosite-youtube', outbound: 'youtube-ru' }],
      ['Telegram мимо geoip-ru', '149.154.167.50', null, { reason: 'default', outbound: 'hy2-bb-direct' }],
      ['частная сеть закрыта', '172.16.0.10', null, { reason: 'reject', routeId: null }],
      ['link-local закрыт', '169.254.169.254', null, { reason: 'reject', routeId: null }],
      ['по умолчанию', 'unknown-site.com', '1.0.0.1', { reason: 'default', rule: '', routeId: null, outbound: 'hy2-bb-direct', delay: 50 }],
      ['домен без адреса - по умолчанию', 'unknown-site.com', null, { reason: 'default', outbound: 'hy2-bb-direct' }],
    ]
    for (const [name, input, ip, want] of cases) {
      it(name, () => {
        expect(check(input, ip)).toMatchObject(want)
      })
    }

    function checkFor(source: string, input: string, ip: string | null = null) {
      const q = rc.parseQuery(input)!
      return rc.explain(plan, rc.clientConn(q, ip, source), ROWS, PROXIES)
    }

    it('клиент с личным выходом идёт в свой экзит', () => {
      expect(checkFor(MAMA, 'unknown-site.com', '1.0.0.1'))
        .toMatchObject({ reason: 'client', rule: '', routeId: null, outbound: 'hy2-aa-direct', delay: 40 })
      expect(check('unknown-site.com', '1.0.0.1')).toMatchObject({ reason: 'default', outbound: 'hy2-bb-direct' })
    })

    it('личный выход ниже ручных правил, YouTube и РФ', () => {
      expect(checkFor(MAMA, 'api.openai.com')).toMatchObject({ reason: 'manual', routeId: 2, outbound: 'hy2-bb-warp' })
      expect(checkFor(MAMA, 'yandex.ru')).toMatchObject({ reason: 'ru', outbound: 'direct-ru' })
      expect(checkFor(MAMA, 'www.youtube.com')).toMatchObject({ reason: 'youtube', outbound: 'youtube-ru' })
    })

    it('клиент без личного выхода идёт своей дорожкой', () => {
      expect(checkFor(OTHER, 'unknown-site.com', '1.0.0.1'))
        .toMatchObject({ reason: 'default', rule: '', routeId: null, outbound: 'hy2-bb-warp', delay: 70 })
    })

    it('ручное правило из одного IP без маски', () => {
      const p = buildPlan([{ id: 7, type: 'ip_cidr', value: '1.1.1.1/32', outbound: 'direct-ru' }], {})
      const q = rc.parseQuery('1.1.1.1')!
      expect(rc.explain(p, rc.clientConn(q, null), [{ id: 7, type: 'ip_cidr', value: '1.1.1.1/32', outbound: 'direct-ru' }], PROXIES))
        .toMatchObject({ reason: 'manual', rule: '1.1.1.1/32', routeId: 7, outbound: 'direct-ru' })
    })

    it('без ответа clash закреплённый выход назван своим именем', () => {
      const q = rc.parseQuery('api.openai.com')!
      expect(rc.explain(plan, rc.clientConn(q, null), ROWS, [])).toMatchObject({ reason: 'manual', routeId: 2, outbound: 'hy2-bb-warp', delay: null })
    })

    it('разбор ввода: ссылка, порт, IDN, мусор', () => {
      expect(rc.parseQuery(' HTTPS://User@Chatgpt.com:8443/c/1?x ')).toEqual({ domain: 'chatgpt.com', shown: 'chatgpt.com' })
      expect(rc.parseQuery('пример.рф')).toEqual({ domain: 'xn--e1afmkfd.xn--p1ai', shown: 'пример.рф' })
      expect(rc.parseQuery('[2001:db8::1]:443')).toEqual({ ip: '2001:db8::1', shown: '2001:db8::1' })
      expect(rc.parseQuery('1.1.1.1')).toEqual({ ip: '1.1.1.1', shown: '1.1.1.1' })
      expect(rc.parseQuery('localhost')).toBeNull()
      expect(rc.parseQuery('   ')).toBeNull()
    })
  })
})
