// Подставные VictoriaMetrics (:8428) и clash API sing-box (:9090) для локального
// прогона e2e. Запускается внутри контейнера панели, поэтому слушает только
// 127.0.0.1. Ноды и выходы — из тех же NUXT_MGMT_IPS / NUXT_EXIT_TAGS, что у панели.
import { readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { DatabaseSync } from 'node:sqlite'

const mgmt = Object.fromEntries(
  (process.env.NUXT_MGMT_IPS || 'ru:10.99.0.1')
    .split(',')
    .map(p => p.split(':'))
    .filter(([t, ip]) => t && ip),
)
const tags = (process.env.NUXT_EXIT_TAGS || '').split(/\s+/).filter(Boolean)
const instances = Object.values(mgmt).map(ip => `${ip}:9100`)

// Число ядер узла: запрос тоже по node_cpu_seconds_total, поэтому стоит раньше
// загрузки CPU и отвечает своим целым числом.
const CORES_QUERY = 'count by(instance)(node_cpu_seconds_total'
const CORES = 2

const SERIES = [
  ['timestamp(', 1],
  [CORES_QUERY, CORES],
  ['node_cpu_seconds_total', 12.5],
  ['MemAvailable', 41],
  ['receive_bytes', 125000],
  ['transmit_bytes', 62500],
  ['boot_time', 3 * 86400 + 3600],
]

function vm(req, res) {
  const url = new URL(req.url, 'http://x')
  if (url.pathname === '/api/v1/query_range') return vmRange(url, res)
  if (url.pathname !== '/api/v1/query') return notFound(res)
  const q = url.searchParams.get('query') || ''
  const now = Number(url.searchParams.get('time')) || Date.now() / 1000
  const hit = SERIES.find(([needle]) => q.includes(needle))
  const result = hit ? instances.map(instance => ({ metric: { instance }, value: [now, String(hit[1])] })) : []
  json(res, { status: 'success', data: { resultType: 'vector', result } }, 'application/json')
}

// Ряды за период: ровная линия из SERIES.
function vmRange(url, res) {
  const q = url.searchParams.get('query') || ''
  const start = Number(url.searchParams.get('start'))
  const end = Number(url.searchParams.get('end'))
  const step = Number.parseFloat(url.searchParams.get('step') || '60') || 60
  const times = []
  for (let t = start; t <= end; t += step) times.push(t)
  const hit = SERIES.find(([needle]) => q.includes(needle))
  const result = Object.values(mgmt).flatMap((ip) => {
    const values = times.flatMap((t) => {
      const v = hit?.[1]
      return v == null ? [] : [[t, String(v)]]
    })
    return values.length ? [{ metric: { instance: `${ip}:9100` }, values }] : []
  })
  json(res, { status: 'success', data: { resultType: 'matrix', result } }, 'application/json')
}

let proxiesMutation = null
let insertedId = null

function clash(req, res) {
  const url = new URL(req.url, 'http://x')
  // Конкурентная запись ровно после проверки БД в PATCH, до ответа sing-box.
  // Не держим HTTP до завершения выпуска ключей/уведомления другого запроса:
  // это проверяло бы таймаут sing-box, а не атомарность изменения клиента.
  if (url.pathname === '/__test/proxies') {
    if (req.method === 'POST') {
      let body = ''
      req.on('data', (chunk) => { body += chunk })
      req.on('end', () => {
        proxiesMutation = JSON.parse(body)
        insertedId = null
        json(res, { insertedId })
      })
      return
    }
    const result = { insertedId }
    if (req.method === 'DELETE') {
      proxiesMutation = null
      insertedId = null
    }
    return json(res, result)
  }
  if (url.pathname === '/proxies') {
    if (proxiesMutation) {
      const mutation = proxiesMutation
      proxiesMutation = null
      const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite')
      try {
        db.exec('PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON')
        const result = mutation.clientName
          ? db.prepare('INSERT INTO clients (name, password) VALUES (?, ?)').run(mutation.clientName, 'e2e-race-password')
          : db.prepare('INSERT INTO devices (client_id, name) VALUES (?, ?)').run(mutation.device.clientId, mutation.device.name)
        insertedId = Number(result.lastInsertRowid)
      }
      finally { db.close() }
    }
    const proxies = { 'direct-ru': { name: 'direct-ru', type: 'Direct', history: [] } }
    tags.forEach((t, i) => {
      for (const v of ['direct', 'warp']) {
        const name = `hy2-${t}-${v}`
        proxies[name] = { name, type: v === 'direct' ? 'Hysteria2' : 'WireGuard', history: [{ time: new Date().toISOString(), delay: 40 + i * 10 }] }
      }
    })
    // 32 дорожки как у gen-router-config: lane-NN смотрит на tags[N % узлов],
    // каждая седьмая — через warp. Ту же раскладку считает monitoring.spec.ts.
    for (let i = 0; tags.length && i < 32; i++) {
      const name = `lane-${String(i).padStart(2, '0')}`
      proxies[name] = { name, type: 'Selector', now: `hy2-${tags[i % tags.length]}-${i % 7 === 0 ? 'warp' : 'direct'}`, history: [] }
    }
    return json(res, { proxies })
  }
  if (url.pathname === '/connections') {
    return json(res, { downloadTotal: 0, uploadTotal: 0, connections: [] })
  }
  notFound(res)
}

// clash API sing-box отдаёт JSON как text/plain — повторяем, панель это
// учитывает; VictoriaMetrics — честный application/json.
function json(res, body, type = 'text/plain; charset=utf-8') {
  res.writeHead(200, { 'content-type': type })
  res.end(JSON.stringify(body))
}

function notFound(res) {
  res.writeHead(404)
  res.end()
}

// Бот (:8877): токен с E2E_ACCEPTED Telegram «принял» — /health 200. Любой
// другой «отвергнут»: настоящий бот тогда ждёт исправления токена, порт открыт,
// но HTTP никто не отвечает. Так и здесь — соединение принято и молчит.
function bot(req, res) {
  let token = ''
  try {
    token = JSON.parse(readFileSync('/var/lib/anysda-vpn2/runtime/telegram-runtime.json', 'utf8')).bot_token || ''
  }
  catch {
    // файла ещё нет — бот не настроен
  }
  if (token.includes('E2E_ACCEPTED')) {
    if (req.url === '/event' || req.url === '/events' || req.url === '/health') return acceptedBot(req, res)
    res.writeHead(404)
    return res.end()
  }
  setTimeout(() => req.socket.destroy(), 5000)
}

// AdGuard Home (:3000): ручки /control/*, которыми пользуется вкладка DNS,
// с состоянием в памяти. Вердикт check_host и журнал считаются по тем же
// правилам, что и у настоящего: свои @@ сильнее списков, сервис сильнее @@.
const agh = {
  user_rules: ['||mc.yandex.ru^'],
  filters: [
    { id: 1, name: 'AdGuard DNS filter', url: 'https://adguardteam.github.io/HostlistsRegistry/assets/filter_1.txt', enabled: true, rules_count: 72410, last_updated: new Date().toISOString() },
  ],
  whitelist_filters: [],
  blocked_services: ['tiktok'],
  rewrites: [{ domain: 'test.lan', answer: '192.0.2.1', enabled: true }],
  dns: {
    upstream_dns: ['https://dns.example/dns-query', '192.0.2.53'],
    bootstrap_dns: ['192.0.2.53'],
    upstream_mode: 'load_balance',
    dnssec_enabled: false,
    disable_ipv6: true,
    cache_size: 4 * 1024 * 1024,
    cache_enabled: true,
    cache_optimistic: false,
    ratelimit: 20,
    blocking_mode: 'default',
    blocking_ipv4: '',
    blocking_ipv6: '',
    protection_enabled: true,
  },
  safebrowsing: false,
  parental: false,
  safesearch: { enabled: false, bing: true, duckduckgo: true, ecosia: true, google: true, pixabay: true, yandex: true, youtube: true },
  querylog: { enabled: true, interval: 2160 * 3_600_000, anonymize_client_ip: false, ignored: [], ignored_enabled: false },
  statsConfig: { enabled: true, interval: 24 * 3_600_000, ignored: [], ignored_enabled: false },
  clients: [{ name: 'Свой клиент', ids: ['10.66.66.200'], use_global_settings: false, filtering_enabled: true, safe_search: { enabled: true }, use_global_blocked_services: false, blocked_services: ['instagram'] }],
}
let aghNextId = 1_700_000_000
// Движок фильтрации: как у настоящего, set_rules и set_url видны в
// filtering/status сразу, а check_host и журнал — после пересборки (~0,4 с).
const aghSnap = () => ({ user_rules: [...agh.user_rules], on: new Set(agh.filters.filter(f => f.enabled).map(f => f.id)) })
let aghApplied = aghSnap()
let aghRebuild
function aghApply() {
  clearTimeout(aghRebuild)
  aghRebuild = setTimeout(() => { aghApplied = aghSnap() }, 400)
}
// Что «содержат» списки: домен → id списка.
const AGH_LISTS = { 'ads.example.net': 1, 'app-measurement.com': 1, 'doubleclick.net': 1 }
const AGH_SERVICES = [
  { id: 'youtube', name: 'YouTube', domains: ['youtube.com', 'googlevideo.com'] },
  { id: 'tiktok', name: 'TikTok', domains: ['tiktok.com', 'tiktokcdn.com'] },
  { id: 'instagram', name: 'Instagram', domains: ['instagram.com'] },
]
const under = (name, d) => name === d || name.endsWith(`.${d}`)

function aghCheck(name) {
  const svc = AGH_SERVICES.find(s => agh.blocked_services.includes(s.id) && s.domains.some(d => under(name, d)))
  if (svc) return { reason: 'FilteredBlockedService', service_name: svc.id, rules: [{ text: `||${name}^`, filter_list_id: -2 }] }
  const rule = (allow) => aghApplied.user_rules.find((r) => {
    const m = r.trim().match(/^(@@)?\|\|([^^$]+)\^/)
    return m && !!m[1] === allow && under(name, m[2])
  })
  const allow = rule(true), deny = rule(false)
  if (deny && (!allow || deny.includes('$important'))) return { reason: 'FilteredBlackList', rules: [{ text: deny, filter_list_id: 0 }] }
  if (allow) return { reason: 'NotFilteredWhiteList', rules: [{ text: allow, filter_list_id: 0 }] }
  const hit = Object.keys(AGH_LISTS).find(d => under(name, d))
  const list = hit && agh.filters.find(f => f.id === AGH_LISTS[hit] && aghApplied.on.has(f.id))
  if (list) return { reason: 'FilteredBlackList', rules: [{ text: `||${hit}^`, filter_list_id: list.id }] }
  return { reason: 'NotFilteredNotFound', rules: [] }
}

// Адреса устройств панели: AdGuard видит клиентов адресами в туннеле. Берутся
// на каждый запрос, новые первыми: устройство, заведённое тестом, сразу в журнале.
function aghClientIps() {
  try {
    const db = new DatabaseSync('/var/lib/anysda-vpn2/db.sqlite', { readOnly: true })
    try { return db.prepare('SELECT wg_ip FROM devices WHERE wg_ip IS NOT NULL ORDER BY id DESC LIMIT 4').all().map(r => r.wg_ip) }
    finally { db.close() }
  }
  catch { return [] }
}

// Журнал пишется один раз, как у настоящего: правило, добавленное позже, старые
// строки не меняет.
const AGH_ASKED = ['ads.example.net', 'github.com', 'mc.yandex.ru', 'www.youtube.com', 'vm.tiktok.com', 'chatgpt.com', 'doubleclick.net', 'test.lan']
let aghLog = null
function aghJournal() {
  if (aghLog) return aghLog
  const now = Date.now()
  aghLog = Array.from({ length: 64 }, (_, i) => {
    const name = AGH_ASKED[i % AGH_ASKED.length]
    const c = aghCheck(name)
    const rw = name === 'test.lan'
    return {
      time: new Date(now - i * 37_000).toISOString(),
      question: { name, type: i % 5 ? 'A' : 'AAAA', class: 'IN' },
      client_info: null,
      reason: rw ? 'Rewrite' : c.reason,
      rules: rw ? [] : c.rules,
      service_name: c.service_name,
      answer: rw ? [{ type: 'A', value: '192.0.2.1', ttl: 10 }] : [{ type: 'A', value: '203.0.113.7', ttl: 300 }],
      cached: !rw && c.reason === 'NotFilteredNotFound' && i % 3 === 0,
      upstream: c.reason === 'NotFilteredNotFound' ? 'https://dns.example:443/dns-query' : '',
      elapsedMs: String(3 + (i % 7) * 0.9),
    }
  })
  return aghLog
}

function aghStats() {
  const q = Array.from({ length: 168 }, (_, i) => i < 144 ? 0 : 60 + Math.round(40 * Math.sin(i / 3)))
  const b = q.map(v => Math.round(v * 0.12))
  const top = obj => Object.entries(obj).map(([k, v]) => ({ [k]: v }))
  const ips = aghClientIps()
  return {
    time_units: 'hours',
    num_dns_queries: q.slice(-24).reduce((a, x) => a + x, 0),
    num_blocked_filtering: b.slice(-24).reduce((a, x) => a + x, 0),
    num_replaced_safebrowsing: 2,
    num_replaced_safesearch: 0,
    num_replaced_parental: 0,
    avg_processing_time: 0.0142,
    dns_queries: q,
    blocked_filtering: b,
    replaced_safebrowsing: q.map((_, i) => i === 160 || i === 165 ? 1 : 0),
    replaced_parental: q.map(() => 0),
    top_queried_domains: top({ 'github.com': 120, 'chatgpt.com': 80, 'ads.example.net': 40 }),
    top_clients: top(Object.fromEntries((ips.length ? ips : ['10.66.66.2']).map((ip, i) => [ip, 300 - i * 50]))),
    top_blocked_domains: top({ 'mc.yandex.ru': 31, 'ads.example.net': 19, 'doubleclick.net': 11 }),
    top_upstreams_responses: top({ 'https://dns.example:443/dns-query': 700, '192.0.2.53:53': 300 }),
    top_upstreams_avg_time: top({ 'https://dns.example:443/dns-query': 0.0041, '192.0.2.53:53': 0.0072 }),
  }
}

function aghBody(req) {
  return new Promise((resolve) => {
    let body = ''
    req.on('data', (c) => { body += c })
    req.on('end', () => {
      try { resolve(JSON.parse(body || '{}')) }
      catch { resolve(null) }
    })
  })
}

async function adguard(req, res) {
  const url = new URL(req.url, 'http://agh')
  const path = url.pathname.replace(/^\/control\//, '')
  const ok = () => { res.writeHead(200); res.end('OK') }
  if (req.method === 'GET') {
    if (path === 'stats') return json(res, aghStats(), 'application/json')
    if (path === 'dns_info') return json(res, agh.dns, 'application/json')
    if (path === 'rewrite/list') return json(res, agh.rewrites.length ? agh.rewrites : null, 'application/json')
    if (path === 'blocked_services/get') return json(res, { ids: agh.blocked_services, schedule: { time_zone: 'UTC' } }, 'application/json')
    if (path === 'clients') return json(res, { clients: agh.clients, auto_clients: [], supported_tags: [] }, 'application/json')
    if (path === 'safebrowsing/status') return json(res, { enabled: agh.safebrowsing }, 'application/json')
    if (path === 'parental/status') return json(res, { enabled: agh.parental }, 'application/json')
    if (path === 'safesearch/status') return json(res, agh.safesearch, 'application/json')
    if (path === 'querylog/config') return json(res, agh.querylog, 'application/json')
    if (path === 'stats/config') return json(res, agh.statsConfig, 'application/json')
    if (path === 'filtering/status') {
      const { user_rules, filters, whitelist_filters } = agh
      return json(res, { enabled: true, interval: 24, user_rules, filters, whitelist_filters }, 'application/json')
    }
    if (path === 'filtering/check_host') return json(res, aghCheck(url.searchParams.get('name') ?? ''), 'application/json')
    if (path === 'blocked_services/all') {
      return json(res, { blocked_services: AGH_SERVICES.map(({ id, name }) => ({ id, name, icon_svg: '', rules: [] })), groups: [] }, 'application/json')
    }
    if (path === 'querylog') {
      const p = url.searchParams
      const status = p.get('response_status') ?? 'all'
      const s = p.get('search') ?? ''
      const older = p.get('older_than')
      const limit = Number(p.get('limit') ?? 500)
      const ips = aghClientIps()
      const all = aghJournal().map((e, i) => ({ ...e, client: ips[i % Math.max(1, ips.length)] ?? `10.66.66.${2 + i % 3}` }))
        .filter(e => (!older || e.time < older)
        && (status !== 'filtered' || e.reason.startsWith('Filtered') || e.reason === 'Rewrite')
        && (!s || e.question.name.includes(s) || e.client.includes(s)))
      const data = all.slice(0, limit)
      return json(res, { data, oldest: data.at(-1)?.time ?? '' }, 'application/json')
    }
  }
  if (req.method === 'POST' && path.startsWith('filtering/') && path !== 'filtering/set_rules') {
    const b = await aghBody(req)
    const key = b?.whitelist ? 'whitelist_filters' : 'filters'
    const fail = (text) => { res.writeHead(400); res.end(text) }
    if (!b) return fail('bad json')
    if (path === 'filtering/add_url') {
      if (agh[key].some(f => f.url === b.url)) return fail(`Filter with URL "${b.url}": url already exists`)
      // Адрес .invalid AdGuard не скачает — как у настоящего, отказ с текстом.
      if (/\.invalid(\/|$)/.test(new URL(b.url).hostname + '/')) return fail(`Couldn't fetch filter from URL "${b.url}": no addresses for host`)
      agh[key].push({ id: aghNextId++, name: b.name, url: b.url, enabled: true, rules_count: 1200, last_updated: new Date().toISOString() })
      aghApply()
      res.writeHead(200)
      return res.end('OK 1200 rules')
    }
    if (path === 'filtering/set_url') {
      const f = agh[key].find(x => x.url === b.url)
      if (!f) return fail('url doesn\'t exist')
      Object.assign(f, { name: b.data.name, url: b.data.url, enabled: b.data.enabled })
      aghApply()
      return ok()
    }
    if (path === 'filtering/remove_url') {
      agh[key] = agh[key].filter(f => f.url !== b.url)
      aghApply()
      return ok()
    }
    if (path === 'filtering/refresh') {
      for (const f of agh[key]) if (f.enabled) f.last_updated = new Date().toISOString()
      return json(res, { updated: 0 }, 'application/json')
    }
  }
  // Перезаписи, сервисы и настройки: ответы и тексты отказов — как у 0.107.79.
  if (req.method === 'POST' || req.method === 'PUT') {
    const fail = (code, text) => { res.writeHead(code); res.end(text) }
    const same = (a, b) => a.domain === b.domain && a.answer === b.answer
    if (path === 'rewrite/add') {
      const b = await aghBody(req)
      agh.rewrites.push({ domain: b.domain, answer: b.answer, enabled: b.enabled ?? true })
      return ok()
    }
    if (path === 'rewrite/update') {
      const b = await aghBody(req)
      const i = agh.rewrites.findIndex(r => same(r, b.target))
      if (i < 0) return fail(400, 'target rule not found')
      agh.rewrites[i] = { ...b.update }
      return ok()
    }
    if (path === 'rewrite/delete') {
      const b = await aghBody(req)
      agh.rewrites = agh.rewrites.filter(r => !same(r, b))
      return ok()
    }
    if (path === 'blocked_services/update') {
      const b = await aghBody(req)
      const bad = (b.ids ?? []).find(id => !AGH_SERVICES.some(x => x.id === id))
      if (bad) return fail(422, `validating: unknown blocked-service "${bad}"`)
      agh.blocked_services = [...(b.ids ?? [])]
      return ok()
    }
    if (path === 'dns_config') {
      const b = await aghBody(req)
      const i = (b.upstream_dns ?? []).findIndex(u => /\s/.test(u) || !u.trim())
      if (i >= 0) return fail(400, `validating dns config: upstream servers: parsing error at index ${i}: cannot prepare the upstream: invalid address ${b.upstream_dns[i]}`)
      Object.assign(agh.dns, b)
      return ok()
    }
    if (path === 'test_upstream_dns') {
      const b = await aghBody(req)
      // Адрес на .invalid не отвечает — остальные отвечают.
      return json(res, Object.fromEntries((b.upstream_dns ?? []).map(u => [u, /\.invalid(\/|:|$)/.test(u) ? `couldn't communicate with upstream: no addresses for ${u}` : 'OK'])), 'application/json')
    }
    for (const k of ['safebrowsing', 'parental']) {
      if (path === `${k}/enable` || path === `${k}/disable`) {
        agh[k] = path.endsWith('enable')
        return ok()
      }
    }
    if (path === 'safesearch/settings') {
      agh.safesearch = await aghBody(req)
      return ok()
    }
    if (path === 'querylog/config/update') {
      agh.querylog = await aghBody(req)
      return ok()
    }
    if (path === 'stats/config/update') {
      agh.statsConfig = await aghBody(req)
      return ok()
    }
    if (path === 'cache_clear') return ok()
    if (path === 'querylog_clear') {
      aghLog = []
      return ok()
    }
    if (path === 'stats_reset') return ok()
  }
  if (req.method === 'POST' && path === 'filtering/set_rules') {
    const b = await aghBody(req)
    if (!b || !Array.isArray(b.rules)) {
      res.writeHead(400)
      return res.end('bad rules')
    }
    agh.user_rules = b.rules
    aghApply()
    return ok()
  }
  notFound(res)
}

// Бот с принятым токеном, как настоящий, называет в /health своё имя; события
// панели (POST /event) копит журналом, GET /events отдаёт его.
const events = []
function acceptedBot(req, res) {
  if (req.url === '/health') return json(res, { ok: true, username: 'demo_vpn_bot' }, 'application/json')
  if (req.url === '/events') return json(res, events, 'application/json')
  if (req.method === 'POST' && req.url === '/event') {
    let body = ''
    req.on('data', (c) => { body += c })
    req.on('end', () => {
      try {
        const event = JSON.parse(body)
        if (!events.some(saved => event.deliveryId && saved.deliveryId === event.deliveryId)) {
          events.push({ at: new Date().toISOString(), ...event })
        }
        if (events.length > 500) events.shift()
        return json(res, { ok: true, delivery_id: event.deliveryId }, 'application/json')
      }
      catch {
        // не JSON - пропускаем
      }
      res.writeHead(400)
      res.end()
    })
    return
  }
  notFound(res)
}

createServer(vm).listen(8428, '127.0.0.1')
createServer(clash).listen(9090, '127.0.0.1')
createServer(bot).listen(8877, '127.0.0.1')
createServer(adguard).listen(3000, '127.0.0.1')
console.log(`fake backends: vm :8428 (${instances.join(' ')}), clash :9090 (${tags.join(' ')}), bot :8877, adguard :3000`)
