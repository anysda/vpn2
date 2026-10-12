// Подставные VictoriaMetrics (:8428) и clash API sing-box (:9090) для локального
// прогона e2e. Запускается внутри контейнера панели, поэтому слушает только
// 127.0.0.1. Ноды и выходы — из тех же NUXT_MGMT_IPS / NUXT_EXIT_TAGS, что у панели.
//
// FAKE_DEMO=1 (e2e/front-test.sh) - живой режим для показа панели: метрики нод
// и задержки выходов меняются во времени, последний экзит деградирован, есть
// AdGuard (:3000), бот всегда «онлайн» и копит события, а устройства из
// FAKE_LIVE_FILE (его пишет front-test-seed.mjs) качают трафик через IKEv2.
import { readFileSync, renameSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'

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
  if (url.pathname !== '/api/v1/query') return notFound(res)
  const q = url.searchParams.get('query') || ''
  const now = Date.now() / 1000
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

function clash(req, res) {
  const url = new URL(req.url, 'http://x')
  if (url.pathname === '/proxies') {
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
    if (req.url === '/event' || req.url === '/events') return demoBot(req, res)
    res.writeHead(req.url === '/health' ? 200 : 404)
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
    const sinceMidnight = Math.max(60, (t + MSK_OFFSET_SEC) % 86400)
    const share = q.includes('receive') ? 1 : 0.38
    return p.mbps * 1e6 / 8 * sinceMidnight * share * 0.55
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

// AdGuard Home /control/stats: запросы копятся с полуночи.
function adguard(req, res) {
  if (!req.url.startsWith('/control/stats')) return notFound(res)
  const t = Date.now() / 1000
  const sinceMidnight = Math.max(60, (t + MSK_OFFSET_SEC) % 86400)
  const total = Math.round(sinceMidnight * 2.7)
  json(res, {
    num_dns_queries: total,
    num_blocked_filtering: Math.round(total * 0.143),
    num_replaced_safebrowsing: 12,
    num_replaced_safesearch: 0,
    num_replaced_parental: 0,
    avg_processing_time: wave(0.0142, 0.004, t, 2),
    top_blocked_domains: [
      { 'mc.yandex.ru': Math.round(total * 0.031) },
      { 'app-measurement.com': Math.round(total * 0.019) },
      { 'ads.vk.com': Math.round(total * 0.011) },
    ],
  }, 'application/json')
}

// Бот всегда «онлайн»; события панели (POST /event) копит журналом, GET /events
// отдаёт его.
const events = []
function demoBot(req, res) {
  if (req.url === '/health') {
    res.writeHead(200)
    return res.end()
  }
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
if (DEMO) {
  createServer(adguard).listen(3000, '127.0.0.1')
  setInterval(writeIkev2Status, 5000)
}
console.log(`fake backends${DEMO ? ' (demo)' : ''}: vm :8428 (${instances.join(' ')}), clash :9090 (${tags.join(' ')}), bot :8877`)
