// Подставные VictoriaMetrics (:8428) и clash API sing-box (:9090) для локального
// прогона e2e. Запускается внутри контейнера панели, поэтому слушает только
// 127.0.0.1. Ноды и выходы — из тех же NUXT_MGMT_IPS / NUXT_EXIT_TAGS, что у панели.
//
// FAKE_DEMO=1 (e2e/front-test.sh) - живой режим для показа панели: метрики нод
// и задержки выходов меняются во времени, последний экзит деградирован, бот всегда «онлайн» и копит события, а устройства из
// FAKE_LIVE_FILE (его пишет front-test-seed.mjs) качают трафик через IKEv2.
import { readFileSync, renameSync, writeFileSync } from 'node:fs'
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
const DEMO = process.env.FAKE_DEMO === '1'

const SERIES = [
  ['timestamp(', 1],
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
  if (DEMO) {
    const result = Object.entries(mgmt).flatMap(([tag, ip]) => {
      const v = demoMetric(tag, q, now)
      return v == null ? [] : [{ metric: { instance: `${ip}:9100` }, value: [now, String(v)] }]
    })
    return json(res, { status: 'success', data: { resultType: 'vector', result } }, 'application/json')
  }
  const hit = SERIES.find(([needle]) => q.includes(needle))
  const result = hit ? instances.map(instance => ({ metric: { instance }, value: [now, String(hit[1])] })) : []
  json(res, { status: 'success', data: { resultType: 'vector', result } }, 'application/json')
}

// Ряды за период: в живом режиме то же значение, что отдал бы query в каждый
// момент сетки, иначе ровная линия из SERIES.
function vmRange(url, res) {
  const q = url.searchParams.get('query') || ''
  const start = Number(url.searchParams.get('start'))
  const end = Number(url.searchParams.get('end'))
  const step = Number.parseFloat(url.searchParams.get('step') || '60') || 60
  const times = []
  for (let t = start; t <= end; t += step) times.push(t)
  const hit = SERIES.find(([needle]) => q.includes(needle))
  const result = Object.entries(mgmt).flatMap(([tag, ip]) => {
    const values = times.flatMap((t) => {
      const v = DEMO ? demoMetric(tag, q, t) : hit?.[1]
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
    if (DEMO) return json(res, { proxies: demoProxies() })
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
    if (DEMO) return json(res, demoConnections())
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
  if (DEMO) return demoBot(req, res)
  let token = ''
  try {
    token = JSON.parse(readFileSync('/etc/anysda/telegram-runtime.json', 'utf8')).bot_token || ''
  }
  catch {
    // файла ещё нет — бот не настроен
  }
  if (token.includes('E2E_ACCEPTED')) {
    if (req.url === '/event' || req.url === '/events' || req.url === '/health') return demoBot(req, res)
    res.writeHead(404)
    return res.end()
  }
  setTimeout(() => req.socket.destroy(), 5000)
}

// ── живой режим ─────────────────────────────────────────────────────────────

const DEGRADED = tags.at(-1)
const MSK_OFFSET_SEC = 3 * 3600

// Плавное колебание вокруг base: два синуса с разными периодами и фазой узла.
function wave(base, amp, t, seed) {
  return base + amp * (0.65 * Math.sin(t / 47 + seed) + 0.35 * Math.sin(t / 13 + seed * 2.3))
}

function seedOf(tag) {
  return [...tag].reduce((a, c) => a + c.charCodeAt(0), 0) % 17
}

// Профили узлов: RU - вход, через него идёт всё; деградированный экзит
// перегружен, упирается в диск и периодически теряет метрики.
function profile(tag) {
  if (tag === 'ru') return { cpu: 34, ram: 52, mbps: 210, disk: [61, 80], upDays: 41.3 }
  if (tag === DEGRADED) return { cpu: 86, ram: 91, mbps: 38, disk: [1.6, 25], upDays: 0.4, iowait: 17, steal: 13 }
  const s = seedOf(tag)
  return { cpu: 18 + s, ram: 38 + s, mbps: 70 + 4 * s, disk: [14 + s, 25], upDays: 9 + s }
}

function demoMetric(tag, q, t) {
  const p = profile(tag)
  const s = seedOf(tag)
  const clamp = v => Math.max(0, Math.min(100, v))
  // Деградированный узел раз в две минуты на 20 с перестаёт отдавать метрики:
  // карточка краснеет, потом приходит в себя.
  const flap = tag === DEGRADED && t % 120 < 20
  if (q.includes('timestamp(')) return flap ? 6 + (t % 120) / 4 : 0.4 + (t % 2)
  if (q.includes('increase(') || q.includes('increase_prometheus(')) {
    // окно запроса: с полуночи МСК (суточный трафик) или час/сутки рядов обзора
    const sinceMidnight = Math.max(60, (t + MSK_OFFSET_SEC) % 86400)
    const win = Number(/\[(\d+)s\]/.exec(q)?.[1]) || sinceMidnight
    const share = q.includes('receive') ? 1 : 0.38
    // внутри суток трафик дышит: ночью меньше, вечером пик
    const hour = (t + MSK_OFFSET_SEC) / 3600 % 24
    const day = win <= 3600 ? 0.3 + 0.7 * Math.max(0, Math.sin((hour - 7) / 15 * Math.PI)) : 1
    return p.mbps * 1e6 / 8 * Math.max(60, win) * share * 0.55 * day
  }
  if (q.includes('mode="iowait"')) return clamp(wave(p.iowait ?? 0.6, p.iowait ? 6 : 0.4, t, s))
  if (q.includes('mode="steal"')) return clamp(wave(p.steal ?? 0.3, p.steal ? 5 : 0.2, t, s + 1))
  if (q.includes('node_cpu_seconds_total')) return clamp(wave(p.cpu, tag === DEGRADED ? 9 : 12, t, s))
  if (q.includes('MemAvailable')) return clamp(wave(p.ram, 3, t / 4, s))
  if (q.includes('receive_bytes')) return Math.max(0, wave(p.mbps, p.mbps * 0.45, t, s)) * 1e6 / 8
  if (q.includes('transmit_bytes')) return Math.max(0, wave(p.mbps * 0.4, p.mbps * 0.2, t, s + 3)) * 1e6 / 8
  if (q.includes('boot_time')) return p.upDays * 86400 + (t - START)
  if (q.includes('filesystem_avail')) return p.disk[0] * 1024 ** 3
  if (q.includes('filesystem_size')) return p.disk[1] * 1024 ** 3
  return null
}

const START = Date.now() / 1000

function demoProxies() {
  const t = Date.now() / 1000
  const hist = delay => [{ time: new Date().toISOString(), delay: Math.round(delay) }]
  const proxies = { 'direct-ru': { name: 'direct-ru', type: 'Direct', history: [] } }
  for (const tag of tags) {
    const s = seedOf(tag)
    const bad = tag === DEGRADED
    proxies[`hy2-${tag}-direct`] = {
      name: `hy2-${tag}-direct`,
      type: 'Hysteria2',
      history: hist(bad ? wave(430, 90, t, s) : wave(38 + 3 * s, 8, t, s)),
    }
    // warp деградированного узла не отвечает: истории проверок нет
    proxies[`hy2-${tag}-warp`] = {
      name: `hy2-${tag}-warp`,
      type: 'WireGuard',
      history: bad ? [] : hist(wave(95 + 4 * s, 20, t, s + 5)),
    }
    proxies[`hy2-${tag}-mgmt`] = { name: `hy2-${tag}-mgmt`, type: 'Hysteria2', history: [] }
  }
  const healthy = tags.filter(t => t !== DEGRADED)
  const best = healthy[0] ?? tags[0]
  proxies['foreign-best'] = { name: 'foreign-best', type: 'URLTest', now: `hy2-${best}-direct`, history: [] }
  // Дорожки: здоровые узлы делят почти всё, на деградированный смотрит каждая
  // восьмая; каждая седьмая - через warp.
  for (let i = 0; tags.length && i < 32; i++) {
    const tag = i % 8 === 7 || !healthy.length ? DEGRADED : healthy[i % healthy.length]
    const name = `lane-${String(i).padStart(2, '0')}`
    const warp = i % 7 === 0 && tag !== DEGRADED
    proxies[name] = { name, type: 'Selector', now: `hy2-${tag}-${warp ? 'warp' : 'direct'}`, history: [] }
  }
  return proxies
}

// Устройства «в сети» и их скорость - из файла, который пишет
// front-test-seed.mjs: { devices: [{ id, username, ip, exit, rateBps }] }.
function liveDevices() {
  try {
    return JSON.parse(readFileSync(process.env.FAKE_LIVE_FILE || '', 'utf8')).devices ?? []
  }
  catch {
    return []
  }
}

const HOSTS = ['www.youtube.com', 'rr3---sn-4g5e6nsz.googlevideo.com', 'api.openai.com', 'web.telegram.org',
  'www.instagram.com', 'discord.com', 'github.com', 'www.netflix.com', 'chatgpt.com', 'x.com']

function demoConnections() {
  const connections = []
  for (const d of liveDevices()) {
    const n = 2 + (d.id % 4)
    for (let k = 0; k < n; k++) {
      const host = HOSTS[(d.id * 3 + k) % HOSTS.length]
      connections.push({
        chains: [`hy2-${d.exit}-direct`],
        upload: Math.round(d.rateBps * 20 * (k + 1)),
        download: Math.round(d.rateBps * 60 * (k + 1)),
        metadata: {
          destinationIP: '', destinationPort: '443', host, network: 'tcp',
          sourceIP: d.ip, sourcePort: String(40000 + d.id * 10 + k), type: 'Tun',
        },
      })
    }
  }
  return { downloadTotal: 0, uploadTotal: 0, memory: 0, connections }
}

// IKEv2: счётчики живых SA, как их каждые 10 с пишет anysda-ikev2-sync на
// хосте. Сборщик трафика панели берёт из них дельты, отсюда «в сети», трафик
// за сутки и ТОП. Счётчик SA только растёт.
const counters = new Map()
let lastTick = Date.now()
function writeIkev2Status() {
  const now = Date.now()
  const dt = (now - lastTick) / 1000
  lastTick = now
  const sessions = liveDevices().map((d) => {
    const c = counters.get(d.id) ?? { rx: d.rateBps * 20, tx: d.rateBps * 4 }
    const k = Math.max(0.1, wave(1, 0.7, now / 1000, d.id))
    c.rx += d.rateBps * dt * k
    c.tx += d.rateBps * dt * k * 0.18
    counters.set(d.id, c)
    return { uniqueid: String(1000 + d.id), username: d.username, rx: Math.round(c.rx), tx: Math.round(c.tx) }
  })
  if (!sessions.length) return
  const file = '/etc/anysda/ikev2-status.json'
  writeFileSync(`${file}.tmp`, JSON.stringify({ sessions }))
  renameSync(`${file}.tmp`, file)
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
  upstream_mode: 'load_balance',
}
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
    if (path === 'dns_info') return json(res, { upstream_mode: agh.upstream_mode, protection_enabled: true }, 'application/json')
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

// Бот всегда «онлайн» и, как настоящий, называет в /health своё имя; события
// панели (POST /event) копит журналом, GET /events отдаёт его.
const events = []
function demoBot(req, res) {
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
if (DEMO) {
  setInterval(writeIkev2Status, 5000)
}
console.log(`fake backends${DEMO ? ' (demo)' : ''}: vm :8428 (${instances.join(' ')}), clash :9090 (${tags.join(' ')}), bot :8877, adguard :3000`)
