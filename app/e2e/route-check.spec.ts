import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test, expect } from '@playwright/test'
import type { ClashProxy } from '../server/utils/clash-client'
import type { RoutePlan } from '../server/utils/route-match'
import { serverModule } from './support/server-module'

type RouteCheck = typeof import('../server/utils/route-check')
type Row = { id: number, type: string, value: string, outbound: string }

const infraTests = fileURLToPath(new URL('../../infra/tests', import.meta.url))

// Снимок строит тот же скрипт роутера, что пишет его на сервере: генератор
// конфига и anysda-apply-routes.py из стадии 20. Проверки sing-box в нём
// заменены на `true`, наборы geoip/geosite - тестовые.
const BUILD = `
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import router_support as rs
d, a = Path(sys.argv[2]), json.loads(sys.argv[3])
script = rs.prepare(d, **a['env'])
for tag, rules in a['sets'].items():
    (d / (tag + '.json')).write_text(json.dumps({'version': 2, 'rules': rules}))
(d / 'manual.json').write_text(json.dumps(a['rows']))
r = rs.apply(script)
if r.returncode:
    sys.exit(r.stderr)
`

function buildPlan(rows: Row[], sets: Record<string, object[]>, env: Record<string, string> = {}): RoutePlan {
  const dir = mkdtempSync(join(tmpdir(), 'route-plan-'))
  try {
    execFileSync('python3', ['-c', BUILD, infraTests, dir, JSON.stringify({ rows, sets, env })], {
      env: { ...process.env, SING_BOX_BIN: 'true' },
    })
    return JSON.parse(readFileSync(join(dir, 'plan.json'), 'utf8'))
  }
  finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

function checker(): RouteCheck {
  const match = serverModule(new URL('../server/utils/route-match.ts', import.meta.url), {})
  return serverModule<RouteCheck>(new URL('../server/utils/route-check.ts', import.meta.url), { './route-match': match })
}

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
]

test.describe('проверка адреса по снимку маршрутов роутера', () => {
  let plan: RoutePlan
  let rc: RouteCheck
  test.beforeAll(() => {
    plan = buildPlan(ROWS, SETS, { YT_ROUTE: 'zapret' })
    rc = checker()
  })

  function check(input: string, ip: string | null = null) {
    const q = rc.parseQuery(input)!
    return rc.explain(plan, rc.clientConn(q, ip), ROWS, PROXIES)
  }

  const cases: Array<[string, string, string | null, Partial<ReturnType<RouteCheck['explain']>>]> = [
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
    ['частная сеть', '172.16.0.10', null, { reason: 'ru', rule: 'ip_is_private', outbound: 'direct-ru' }],
    ['по умолчанию', 'unknown-site.com', '1.0.0.1', { reason: 'default', rule: '', routeId: null, outbound: 'hy2-bb-direct', delay: 50 }],
    ['домен без адреса - по умолчанию', 'unknown-site.com', null, { reason: 'default', outbound: 'hy2-bb-direct' }],
  ]
  for (const [name, input, ip, want] of cases) {
    test(name, () => {
      expect(check(input, ip)).toMatchObject(want)
    })
  }

  test('ручное правило из одного IP без маски', () => {
    const p = buildPlan([{ id: 7, type: 'ip_cidr', value: '1.1.1.1/32', outbound: 'direct-ru' }], {})
    const q = rc.parseQuery('1.1.1.1')!
    expect(rc.explain(p, rc.clientConn(q, null), [{ id: 7, type: 'ip_cidr', value: '1.1.1.1/32', outbound: 'direct-ru' }], PROXIES))
      .toMatchObject({ reason: 'manual', rule: '1.1.1.1/32', routeId: 7, outbound: 'direct-ru' })
  })

  test('без ответа clash закреплённый выход назван своим именем', () => {
    const q = rc.parseQuery('api.openai.com')!
    expect(rc.explain(plan, rc.clientConn(q, null), ROWS, [])).toMatchObject({ reason: 'manual', routeId: 2, outbound: 'hy2-bb-warp', delay: null })
  })

  test('разбор ввода: ссылка, порт, IDN, мусор', () => {
    expect(rc.parseQuery(' HTTPS://User@Chatgpt.com:8443/c/1?x ')).toEqual({ domain: 'chatgpt.com', shown: 'chatgpt.com' })
    expect(rc.parseQuery('пример.рф')).toEqual({ domain: 'xn--e1afmkfd.xn--p1ai', shown: 'пример.рф' })
    expect(rc.parseQuery('[2001:db8::1]:443')).toEqual({ ip: '2001:db8::1', shown: '2001:db8::1' })
    expect(rc.parseQuery('1.1.1.1')).toEqual({ ip: '1.1.1.1', shown: '1.1.1.1' })
    expect(rc.parseQuery('localhost')).toBeNull()
    expect(rc.parseQuery('   ')).toBeNull()
  })
})
