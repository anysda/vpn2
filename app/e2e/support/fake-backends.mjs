// Подставные VictoriaMetrics (:8428) и clash API sing-box (:9090) для локального
// прогона e2e. Запускается внутри контейнера панели, поэтому слушает только
// 127.0.0.1. Ноды и выходы — из тех же NUXT_MGMT_IPS / NUXT_EXIT_TAGS, что у панели.
import { readFileSync } from 'node:fs'
import { createServer } from 'node:http'

const mgmt = Object.fromEntries(
  (process.env.NUXT_MGMT_IPS || 'ru:10.99.0.1')
    .split(',')
    .map(p => p.split(':'))
    .filter(([t, ip]) => t && ip),
)
const tags = (process.env.NUXT_EXIT_TAGS || '').split(/\s+/).filter(Boolean)
const instances = Object.values(mgmt).map(ip => `${ip}:9100`)

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
  const hit = SERIES.find(([needle]) => q.includes(needle))
  const now = Date.now() / 1000
  const result = hit ? instances.map(instance => ({ metric: { instance }, value: [now, String(hit[1])] })) : []
  json(res, { status: 'success', data: { resultType: 'vector', result } }, 'application/json')
}

function clash(req, res) {
  const url = new URL(req.url, 'http://x')
  if (url.pathname === '/proxies') {
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
  if (url.pathname === '/connections') return json(res, { downloadTotal: 0, uploadTotal: 0, connections: [] })
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
    token = JSON.parse(readFileSync('/etc/anysda/telegram-runtime.json', 'utf8')).bot_token || ''
  }
  catch {
    // файла ещё нет — бот не настроен
  }
  if (token.includes('E2E_ACCEPTED')) {
    res.writeHead(req.url === '/health' ? 200 : 404)
    return res.end()
  }
  setTimeout(() => req.socket.destroy(), 5000)
}

createServer(vm).listen(8428, '127.0.0.1')
createServer(clash).listen(9090, '127.0.0.1')
createServer(bot).listen(8877, '127.0.0.1')
console.log(`fake backends: vm :8428 (${instances.join(' ')}), clash :9090 (${tags.join(' ')}), bot :8877`)
