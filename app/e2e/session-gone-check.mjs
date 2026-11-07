// Запросы панели, когда cookie сессии исчезла на живой странице: без
// браузера и контейнера. Настоящие обёртки fetch из app/plugins/*.client.ts
// и настоящие server/middleware/*.ts; браузер и сервер подставные: банка
// cookie, где httpOnly странице не видна, и ответ 401 на запрос без сессии.
//
//   node --test e2e/session-gone-check.mjs
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { beforeEach, test } from 'node:test'

const APP = join(import.meta.dirname, '..')
const ts = createRequire(join(APP, 'package.json'))('typescript')
const SESSION = 'better-auth.session_token'
const OPEN = /^\/api\/(auth\/|sso$|version$)/

/** Модуль .ts без импортов: снять типы и загрузить. */
async function load(file) {
  const src = readFileSync(file, 'utf8')
  const { outputText } = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } })
  return (await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)).default
}

/** Банка cookie браузера. */
const jar = new Map()
const header = () => [...jar].map(([k, v]) => `${k}=${v.value}`).join('; ')

/** Сервер: middleware в порядке Nitro (по имени файла), затем ручка. */
const middleware = []
async function serve(method, path) {
  const event = { method, path, headers: { cookie: header(), host: 'panel.test' }, node: { req: { headers: {} }, res: {} } }
  for (const handler of middleware) await handler(event)
  return OPEN.test(path) || jar.has(SESSION) ? 200 : 401
}

Object.assign(globalThis, {
  defineNuxtPlugin: f => f,
  defineEventHandler: f => f,
  useRuntimeConfig: () => ({ session: { cookie: { secure: false } } }),
  getRequestHeader: (event, name) => event.headers[name.toLowerCase()],
  parseCookies: event => Object.fromEntries(event.headers.cookie.split('; ').filter(Boolean).map(c => c.split(/=(.*)/s).slice(0, 2))),
  setCookie: (event, name, value, opts = {}) => { jar.set(name, { value, httpOnly: !!opts.httpOnly }) },
  deleteCookie: (event, name) => { jar.delete(name) },
  sendRedirect: () => undefined,
  createError: o => Object.assign(new Error(o.message ?? o.statusMessage), o),
  location: { origin: 'http://panel.test', pathname: '/settings' },
  document: {
    get cookie() {
      return [...jar].filter(([, v]) => !v.httpOnly).map(([k, v]) => `${k}=${v.value}`).join('; ')
    },
    set cookie(line) {
      const [pair, ...attrs] = line.split(';').map(s => s.trim())
      const [name, value] = pair.split(/=(.*)/s)
      const gone = attrs.some(a => /^max-age=(0|-)/i.test(a) || (/^expires=/i.test(a) && Date.parse(a.slice(8)) <= Date.now()))
      if (gone) jar.delete(name)
      else jar.set(name, { value, httpOnly: false })
    },
  },
})

const sent = []
globalThis.fetch = async (input, init = {}) => {
  const url = new URL(input instanceof Request ? input.url : String(input), location.origin)
  const method = init.method ?? 'GET'
  sent.push(`${method} ${url.pathname}`)
  const status = await serve(method, url.pathname)
  // Ответ входа ставит cookie сессии, ответ выхода её удаляет.
  if (status === 200 && /^\/api\/auth\/(sign-in\/username|two-factor\/verify-totp)$/.test(url.pathname)) jar.set(SESSION, { value: 's2', httpOnly: true })
  if (status === 200 && url.pathname === '/api/auth/sign-out') jar.delete(SESSION)
  return new Response('{}', { status, headers: { 'content-type': 'application/json' } })
}

for (const f of readdirSync(join(APP, 'server/middleware')).sort()) middleware.push(await load(join(APP, 'server/middleware', f)))
// Плагины Nuxt ставятся по имени файла: позже поставленная обёртка — внешняя.
for (const f of readdirSync(join(APP, 'app/plugins')).filter(f => f.endsWith('.client.ts')).sort()) {
  const file = join(APP, 'app/plugins', f)
  if (readFileSync(file, 'utf8').includes('globalThis.fetch =')) (await load(file))()
}

async function request(path, method = 'GET') {
  const before = sent.length
  const res = await fetch(path, { method })
  return { status: res.status, sent: sent.length > before }
}

/** Вход, затем загрузка страницы настроек с сервера. */
async function signedIn() {
  await request('/api/auth/sign-in/username', 'POST')
  await serve('GET', '/settings')
}

beforeEach(() => {
  jar.clear()
  sent.length = 0
})

test('вошёл: опросы админки уходят и получают ответ', async () => {
  await signedIn()
  assert.deepEqual(await request('/api/admin/telegram'), { status: 200, sent: true })
  assert.deepEqual(await request('/api/admin/bot-deliveries'), { status: 200, sent: true })
})

test('сессия до обновления панели, без признака: загрузка страницы его ставит, опросы идут', async () => {
  jar.set(SESSION, { value: 'old', httpOnly: true })
  await serve('GET', '/')
  assert.deepEqual(await request('/api/admin/telegram'), { status: 200, sent: true })
})

test('cookie стёрты на живой странице: запросы админки не уходят без сессии', async () => {
  await signedIn()
  jar.clear()
  for (const path of ['/api/admin/telegram', '/api/admin/bot-deliveries', '/api/ops/nodes']) {
    const r = await request(path)
    assert.equal(r.sent, false, `${path} ушёл без сессии и получил ${r.status}`)
    assert.equal(r.status, 401, `${path}: странице — тот же отказ, что дал бы сервер`)
  }
  assert.deepEqual(await request('/api/admin/telegram', 'PUT'), { status: 401, sent: false })
})

test('без сессии открытые ручки уходят: страница входа работает', async () => {
  assert.deepEqual(await request('/api/sso'), { status: 200, sent: true })
  assert.deepEqual(await request('/api/version'), { status: 200, sent: true })
  assert.deepEqual(await request('/api/auth/get-session'), { status: 200, sent: true })
  assert.equal((await request('/api/auth/sign-in/username', 'POST')).sent, true)
})

test('выход: после sign-out опрос админки не уходит, новый вход возвращает опросы', async () => {
  await signedIn()
  assert.equal((await request('/api/auth/sign-out', 'POST')).status, 200)
  assert.deepEqual(await request('/api/admin/telegram'), { status: 401, sent: false })
  await request('/api/auth/sign-in/username', 'POST')
  await request('/api/auth/get-session')
  assert.deepEqual(await request('/api/admin/telegram'), { status: 200, sent: true })
})

test('вход с 2FA: после пароля сессии ещё нет, после кода опросы идут', async () => {
  jar.set('better-auth.two_factor', { value: 't', httpOnly: true })
  assert.deepEqual(await request('/api/admin/telegram'), { status: 401, sent: false })
  await request('/api/auth/two-factor/verify-totp', 'POST')
  await request('/api/auth/get-session')
  assert.deepEqual(await request('/api/admin/telegram'), { status: 200, sent: true })
})
