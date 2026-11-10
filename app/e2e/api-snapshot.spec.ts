import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { APIRequestContext } from '@playwright/test'
import { expect, loginApi, test } from './support/fixtures'
import { container } from './support/openvpn-ca'
// @ts-expect-error модуль .mjs без деклараций: обход запускается и из скрипта
import { walk, snapshotFiles, normalize } from '../scripts/api-snapshot.mjs'

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

test.describe.configure({ mode: 'serial' })

test.describe('снимок ответов API панели', () => {
  test.skip(!container, 'нужен контейнер панели run-local.sh (E2E_FAULT_CONTAINER)')
  test.setTimeout(240_000)

  let actual = new Map<string, string>()
  let walked = false

  test('обход сценария собирает записи', async ({ page }, testInfo) => {
    await loginApi(page)
    const api = page.request
    const raw = await walk({
      admin: (method: string, path: string, data?: unknown) => adminFetch(api, method, path, data),
      bot: async (method: string, path: string, data: unknown, auth: boolean) => botFetch(method, path, data, auth),
      settled: async (id: string) => {
        await expect.poll(async () => (await (await api.get(`/api/access-operations/${id}`)).json()).status,
          { timeout: 45_000 }).toBe('confirmed')
      },
      gone: async (path: string) => {
        await expect.poll(async () => (await api.get(path)).status(), { timeout: 45_000 }).toBe(404)
      },
    })
    // Сырой обход - в отчёт: по нему уточняется таблица нормализации.
    await testInfo.attach('raw.json', { body: JSON.stringify([...raw], null, 2), contentType: 'application/json' })
    actual = snapshotFiles(raw)
    walked = true
    await testInfo.attach('actual.json', { body: JSON.stringify([...actual], null, 2), contentType: 'application/json' })
    expect(normalize).toBeTypeOf('function')
  })

  test('каждому маршруту есть запись и каждой записи есть маршрут', () => {
    expect(recorded()).toEqual(routes())
    expect(walked, 'обход должен был отработать').toBe(true)
    expect([...actual.keys()].sort()).toEqual(routes())
  })

  test('записи совпадают с эталоном байт в байт', async (_fixtures, testInfo) => {
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
