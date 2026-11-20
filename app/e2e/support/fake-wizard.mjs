// Подставной сервер мастера установки по договору vpn2.wizard (contracts.md, «contract: wiz»):
// статика wizard/dist и /api/*. Первой строкой stdout - {port, token}. POST /__set меняет его
// состояние из теста: {phase, job, status, checks: {host: [problems]}, expired, offline: ms, draft, secrets, backup_key}.
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { extname, join, normalize } from 'node:path'

const dist = process.argv[2]
const token = 'fixture-token-only-for-e2e-0123456789abcdef'
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2' }
const EXPIRED = 'Ссылка устарела - запустите ./deploy.sh ещё раз'
const sessions = new Set()
let offlineUntil = 0
let expired = false
let checks = {}
let issued = null
let status = { panel_url: 'https://203.0.113.1/', systems: [] }
const state = {
  phase: 'filling',
  draft: {
    step: 1, admin_ssh_key: '', exits: [], panel: { domain: '', admin_user: 'anysda' }, telegram: { enabled: false, chat_id: '' },
    backup: { target: 'local', s3: { endpoint: '', bucket: '', region: '' } }, youtube: false,
  },
  secrets: { admin_password: false, telegram_bot_token: false, s3_access_key: false, s3_secret_key: false },
  backup_key: 'none', orchestrator_key: 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureOrchestratorKeyOnlyForE2e root@entry',
  link_expires_at: Math.floor(Date.now() / 1000) + 3600, job: null,
}

const refuse = (res, code, refused) => send(res, code, { refused })
function send(res, code, body) {
  res.writeHead(code, { 'content-type': 'application/json', 'cache-control': 'no-store' })
  res.end(JSON.stringify(body))
}
async function body(req) {
  let text = ''
  for await (const chunk of req) text += chunk
  return text ? JSON.parse(text) : {}
}
const cookie = req => /(?:^|;\s*)vpn2_wizard=([^;]+)/.exec(req.headers.cookie ?? '')?.[1]
const good = e => !e.skipped && e.problems?.length === 0
const running = stages => stages.map(s => s.state === 'failed' ? { ...s, state: 'running', failed: [] } : s)
const job = (res, next) => send(res, 200, { job: Object.assign(state.job, next) })

const routes = {
  'POST /api/session': async (req, res) => {
    if ((await body(req)).token !== token || expired) return refuse(res, 401, EXPIRED)
    const sid = Math.random().toString(36).slice(2)
    sessions.add(sid)
    res.setHeader('set-cookie', `vpn2_wizard=${sid}; HttpOnly; SameSite=Strict; Path=/`)
    send(res, 200, {})
  },
  'GET /api/state': (req, res) => send(res, 200, state),
  'PUT /api/draft': async (req, res) => {
    if (state.phase !== 'filling') return refuse(res, 409, 'Установка уже идёт: черновик больше не меняется')
    const patch = await body(req)
    if (patch.exits) {
      const tags = patch.exits.map(e => e.tag)
      const bad = patch.exits.find(e => !/^[a-z][a-z0-9]{0,7}$/.test(e.tag))
      if (bad) return refuse(res, 400, `Тег «${bad.tag}»: латиница и цифры, до 8 знаков, с буквы`)
      const twice = tags.find((t, i) => tags.indexOf(t) !== i)
      if (twice) return refuse(res, 400, `Тег «${twice}» уже есть в списке экзитов`)
      // problems ставит только сервер: прежний итог проверки, пока тег и адрес те же.
      // Как vpn2.wizard: адрес без root@ (вход всегда root); problems ставит только сервер -
      // прежний итог проверки, пока тег и адрес те же.
      patch.exits = patch.exits.map(({ tag, host: typed, skipped }) => {
        const host = typed.trim().replace(/^root@/, '')
        return { tag, host, skipped: !!skipped, problems: state.draft.exits.find(e => e.tag === tag && e.host === host)?.problems ?? null }
      })
    }
    Object.assign(state.draft, patch)
    send(res, 200, { draft: state.draft })
  },
  'POST /api/exit/check': async (req, res) => {
    const { tag } = await body(req)
    const exit = state.draft.exits.find(e => e.tag === tag)
    if (!exit) return refuse(res, 400, `Экзита «${tag}» нет в черновике`)
    exit.problems = checks[`root@${exit.host}`] ?? []
    send(res, 200, { problems: exit.problems })
  },
  'POST /api/backup-key': (req, res) => {
    if (state.backup_key === 'seen') return refuse(res, 409, 'Ключ бэкапов уже сохранён')
    const n = Math.floor(Math.random() * 1e6).toString().padStart(6, '0')
    issued = { private_key: `AGE-SECRET-KEY-1FIXTURE${n}QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ`, recipient: `age1fixture${n}` }
    state.backup_key = 'shown'
    send(res, 200, issued)
  },
  'POST /api/backup-key/seen': async (req, res) => {
    if ((await body(req)).recipient !== issued?.recipient) return refuse(res, 409, 'Ключ сменили в другой вкладке: сохраните ключ, показанный там')
    state.backup_key = 'seen'
    send(res, 200, {})
  },
  'POST /api/install': (req, res) => {
    const exits = state.draft.exits
    if (!exits.some(good) || !exits.every(e => e.skipped || good(e))) return refuse(res, 409, 'Нет ни одного годного экзита: установка не начнётся')
    if (state.backup_key !== 'seen') return refuse(res, 409, 'Ключ бэкапов не подтверждён')
    state.phase = 'installing'
    state.link_expires_at = null
    state.job = { id: 1, kind: 'install', tag: '', host: '', state: 'running', stages: [{ name: 'preflight', state: 'running', nodes: [], failed: [] }], log: '', error: '', actions: [], skipped: [], result: null }
    send(res, 200, { job: state.job })
  },
  'GET /api/job': (req, res) => send(res, 200, { job: state.job }),
  'POST /api/job/retry': (req, res) => job(res, { state: 'running', error: '', actions: [], stages: running(state.job.stages) }),
  'POST /api/job/skip': async (req, res) => {
    const { tag } = await body(req)
    job(res, { state: 'running', error: '', actions: [], skipped: [...state.job.skipped, tag], stages: running(state.job.stages) })
  },
  'GET /api/status': (req, res) => send(res, 200, status),
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x')
  if (url.pathname === '/__set') {
    const next = await body(req)
    if ('offline' in next) offlineUntil = Date.now() + next.offline
    if ('expired' in next) expired = next.expired
    if (expired) sessions.clear()
    if (next.checks) checks = next.checks
    if (next.status) status = next.status
    for (const key of ['phase', 'job', 'backup_key']) if (key in next) state[key] = next[key]
    if (next.draft) Object.assign(state.draft, next.draft)
    if (next.secrets) Object.assign(state.secrets, next.secrets)
    return send(res, 200, { state })
  }
  if (url.pathname.startsWith('/api/')) {
    if (Date.now() < offlineUntil) return req.socket.destroy()
    if (url.pathname !== '/api/session' && !sessions.has(cookie(req))) return refuse(res, 401, EXPIRED)
    const secret = /^\/api\/secret\/(admin_password|telegram_bot_token|s3_access_key|s3_secret_key)$/.exec(url.pathname)
    if (secret && req.method === 'PUT') {
      state.secrets[secret[1]] = !!(await body(req)).value
      return send(res, 200, { secrets: state.secrets })
    }
    const route = routes[`${req.method} ${url.pathname}`]
    return route ? route(req, res) : refuse(res, 404, 'Нет такого запроса')
  }
  const path = normalize(url.pathname === '/' ? '/index.html' : url.pathname)
  try {
    const data = await readFile(join(dist, path))
    res.writeHead(200, { 'content-type': TYPES[extname(path)] ?? 'application/octet-stream' })
    res.end(data)
  }
  catch {
    res.writeHead(404).end()
  }
})
server.listen(0, '127.0.0.1', () => console.log(JSON.stringify({ port: server.address().port, token })))
