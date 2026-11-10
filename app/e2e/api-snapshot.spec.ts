import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { APIRequestContext } from '@playwright/test'
import { expect, loginApi, test } from './support/fixtures'
import { container } from './support/openvpn-ca'
// @ts-expect-error модуль .mjs без деклараций: обход запускается и из скрипта
import { walk, snapshotFiles } from '../scripts/api-snapshot.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const fixtures = join(here, 'fixtures', 'api-snapshot')
const serverApi = join(here, '..', 'server', 'api')
const docker = (process.env.DOCKER || 'docker').split(/\s+/)

interface Reply { status: number, type: string, text: string }

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap(e =>
    e.isDirectory() ? files(join(dir, e.name)) : [join(dir, e.name)])
}

/** Маршруты из дерева server/api: bot, admin, clients и ops/bot-snapshot. */
function routes(): string[] {
  const own = ['bot', 'admin', 'clients'].flatMap(d => files(join(serverApi, d)))
  return [...own, join(serverApi, 'ops', 'bot-snapshot.get.ts')]
    .map(f => relative(serverApi, f).replace(/\.ts$/, ''))
    .sort()
}

function recorded(): string[] {
  return files(fixtures).map(f => relative(fixtures, f).replace(/\.json$/, '')).sort()
}

/** Бот-ручки доступны только изнутри контейнера панели с секретом бота. */
function botFetch(method: string, path: string, data: unknown, auth: boolean): Reply {
  const code = `
    const [method, path, data, auth] = process.argv.slice(1).map(JSON.parse);
    const headers = { 'content-type': 'application/json' };
    if (auth) headers.authorization = 'Bearer ' + process.env.NUXT_TGBOT_SECRET;
    const r = await fetch('http://127.0.0.1:31821' + path, { method, headers, body: data === null ? undefined : JSON.stringify(data) });
    console.log(JSON.stringify({ status: r.status, type: r.headers.get('content-type') || '', text: await r.text() }));
  `
  try {
    const out = execFileSync(docker[0]!, [...docker.slice(1), 'exec', container!, 'node', '--input-type=module', '-e', code,
      ...[method, path, data ?? null, auth].map(a => JSON.stringify(a))], { encoding: 'utf8' })
    return JSON.parse(out.trim())
  }
  catch (e) {
    return { status: 0, type: '', text: String(e instanceof Error ? e.message : e) }
  }
}

async function adminFetch(api: APIRequestContext, method: string, path: string, data?: unknown): Promise<Reply> {
  const r = await api.fetch(path, { method, data, failOnStatusCode: false })
  return { status: r.status(), type: r.headers()['content-type'] ?? '', text: await r.text() }
}

const second = () => Math.floor(Date.now() / 1000)
let createdIn: number | null = null

/**
 * created_at клиента хранится с точностью до секунды, а списки сортируются по нему:
 * каждый следующий клиент сценария создаётся в следующей секунде, порядок в списках один.
 */
async function apart<T>(method: string, path: string, data: unknown, call: () => Promise<T>): Promise<T> {
  const name = (data as { name?: unknown } | undefined)?.name
  const creates = method === 'POST' && ['/api/clients', '/api/bot/admin/clients'].includes(path)
    && typeof name === 'string' && name.startsWith('e2e-api-snapshot-')
  if (!creates) return call()
  while (createdIn !== null && second() <= createdIn) await new Promise(r => setTimeout(r, 20))
  const reply = await call()
  createdIn = second()
  return reply
}

test.describe.configure({ mode: 'serial' })

test.describe('снимок ответов API панели', () => {
  test.setTimeout(360_000)

  let actual = new Map<string, string>()
  let walked = false

  test('обход сценария собирает записи', async ({ page }, testInfo) => {
    expect(container, 'нужен контейнер панели run-local.sh (E2E_FAULT_CONTAINER)').toBeTruthy()
    await loginApi(page)
    const api = page.request
    const { calls: raw, ids } = await walk({
      admin: (method: string, path: string, data?: unknown) => apart(method, path, data, () => adminFetch(api, method, path, data)),
      bot: (method: string, path: string, data: unknown, auth: boolean) => apart(method, path, data, async () => botFetch(method, path, data, auth)),
      settled: async (id: string) => {
        await expect.poll(async () => (await (await api.get(`/api/access-operations/${id}`)).json()).status,
          { timeout: 45_000 }).toBe('confirmed')
      },
      gone: async (path: string) => {
        await expect.poll(async () => (await api.get(path)).status(), { timeout: 45_000 }).toBe(404)
      },
      // Первая попытка доставки ждёт бота 1,5 с, повтор через 30 и 60 с:
      // запись очереди - только когда доставки сценария дошли до конца.
      delivered: async (deliveryId: string) => {
        await expect.poll(async () => {
          const list = await (await api.get('/api/admin/bot-deliveries')).json() as { id: string, label: string, status: string }[]
          const own = list.filter(d => d.id === deliveryId || d.label.includes('e2e-api-snapshot-'))
          return own.some(d => d.id === deliveryId) ? own.filter(d => d.status !== 'accepted').map(d => `${d.label}: ${d.status}`) : ['нет рассылки']
        }, { timeout: 100_000, intervals: [1_000] }).toEqual([])
      },
    })
    // Сырой обход - в отчёт: по нему уточняется таблица нормализации.
    await testInfo.attach('raw.json', { body: JSON.stringify([...raw], null, 2), contentType: 'application/json' })
    actual = snapshotFiles(raw, ids)
    walked = true
    await testInfo.attach('actual.json', { body: JSON.stringify([...actual], null, 2), contentType: 'application/json' })
  })

  test('каждому маршруту есть запись и каждой записи есть маршрут', () => {
    expect(recorded()).toEqual(routes())
    expect(walked, 'обход должен был отработать').toBe(true)
    expect([...actual.keys()].sort()).toEqual(routes())
  })

  test('записи совпадают с эталоном байт в байт', async () => {
    const testInfo = test.info()
    expect(walked, 'обход должен был отработать').toBe(true)
    for (const [route, text] of actual) await testInfo.attach(`${route}.json`, { body: text, contentType: 'application/json' })
    for (const [route, text] of actual) {
      const path = join(fixtures, `${route}.json`)
      expect(existsSync(path), `нет эталона ${route}.json`).toBe(true)
      const expected = readFileSync(path, 'utf8')
      expect(text, route).toBe(expected)
    }
  })
})
