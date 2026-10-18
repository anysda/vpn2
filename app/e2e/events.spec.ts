import type { APIRequestContext } from '@playwright/test'
import { ADMIN_USER, NODES, authPost, expect, loginApi, test, uniq } from './support/fixtures'
import { container, inside } from './support/openvpn-ca'
import { waitAccess } from './support/access'

interface FeedRow { id: number, at: number, type: string, level: string, title: string, detail: string }

const ACTOR = `@${ADMIN_USER}`

async function feed(api: APIRequestContext): Promise<FeedRow[]> {
  const r = await api.get('/api/events?limit=100')
  expect(r.status()).toBe(200)
  return r.json()
}

/** Записи журнала, появившиеся после отметки, старые первыми. */
async function since(api: APIRequestContext, lastId: number, match: (r: FeedRow) => boolean = () => true) {
  return (await feed(api)).filter(r => r.id > lastId && match(r)).reverse()
}

async function lastId(api: APIRequestContext) {
  return (await feed(api))[0]?.id ?? 0
}

test.describe('журнал событий', () => {
  test('клиент: создание, заморозка, срок, перевыпуск, удаление — с автором', async ({ page }) => {
    test.setTimeout(180_000)
    await loginApi(page)
    const api = page.request
    const mark = await lastId(api)
    const name = uniq('events')
    const created = await api.post('/api/clients', { data: { name, deviceLimit: null } })
    expect(created.status()).toBe(200)
    const { id } = await created.json()
    const device = await (await api.post(`/api/clients/${id}/devices`, { data: { name: 'phone' } })).json()
    const freeze = await api.patch(`/api/clients/${id}`, { data: { frozenManual: true }, headers: { 'Idempotency-Key': `events-${id}` } })
    expect(freeze.status()).toBe(202)
    const accepted = await freeze.json()
    await waitAccess(api, accepted.operation)
    // Повтор принятого запроса — не новая операция и не второе событие.
    const replay = await api.patch(`/api/clients/${id}`, { data: { frozenManual: true }, headers: { 'Idempotency-Key': `events-${id}` } })
    expect(replay.status()).toBe(202)
    expect((await replay.json()).operation.id).toBe(accepted.operation.id)
    for (const response of [
      () => api.patch(`/api/clients/${id}`, { data: { frozenManual: false } }),
      () => api.patch(`/api/clients/${id}`, { data: { expiresAt: '2031-05-20T12:00:00.000Z' } }),
      () => api.post(`/api/clients/${id}/devices/${device.id}/reissue`),
      () => api.post(`/api/clients/${id}/reissue`),
      () => api.delete(`/api/clients/${id}`),
    ]) {
      const r = await response()
      expect(r.status()).toBe(202)
      await waitAccess(api, (await r.json()).operation)
    }

    const rows = await since(api, mark, r => r.title.endsWith(`· ${name}`))
    expect(rows.map(r => [r.type, r.level, r.title, r.detail])).toEqual([
      ['client_created', 'info', `Клиент создан · ${name}`, ACTOR],
      ['client_frozen', 'info', `Отключение запущено · ${name}`, `1 устройство · вручную · ${ACTOR}`],
      ['access_confirmed', 'info', `Доступ отключён · ${name}`, ACTOR],
      ['client_unfrozen', 'info', `Включение запущено · ${name}`, `1 устройство · вручную · ${ACTOR}`],
      ['access_confirmed', 'info', `Доступ включён · ${name}`, ACTOR],
      ['client_term', 'info', `Срок изменён · ${name}`, `до 20.05.2031 · ${ACTOR}`],
      ['access_confirmed', 'info', `Доступ включён · ${name}`, ACTOR],
      ['access_confirmed', 'info', `Ключи перевыпущены · ${name}`, ACTOR],
      ['access_confirmed', 'info', `Ключи перевыпущены · ${name}`, ACTOR],
      ['access_confirmed', 'info', `Доступ отключён · ${name}`, ACTOR],
    ])
  })

  test('маршрут: добавление, смена выхода, удаление', async ({ page }) => {
    await loginApi(page)
    const api = page.request
    const mark = await lastId(api)
    const value = `${uniq('events')}.example.com`
    const created = await api.post('/api/routes', { data: { value, outbound: 'direct-ru' } })
    expect(created.status()).toBe(200)
    const { id } = await created.json()
    const exit = NODES.find(t => t !== 'ru') ?? 'nl'
    expect((await api.patch(`/api/routes/${id}`, { data: { outbound: `hy2-${exit}-warp` } })).status()).toBe(200)
    expect((await api.delete(`/api/routes/${id}`)).status()).toBe(200)

    const rows = await since(api, mark, r => r.title.endsWith(`· ${value}`))
    const via = `${exit.toUpperCase()} WARP`
    expect(rows.map(r => [r.type, r.title, r.detail])).toEqual([
      ['route_added', `Маршрут добавлен · ${value}`, `через RU · ${ACTOR}`],
      ['route_exit', `Выход маршрута изменён · ${value}`, `RU → ${via} · ${ACTOR}`],
      ['route_deleted', `Маршрут удалён · ${value}`, `был через ${via} · ${ACTOR}`],
    ])
  })

  test('экзит выключен и включён вручную — по записи на переход', async ({ page }) => {
    const exits = NODES.filter(t => t !== 'ru')
    test.skip(exits.length < 2, 'нужно два экзита')
    await loginApi(page)
    const api = page.request
    const tag = exits[0]!
    const mark = await lastId(api)
    try {
      expect((await api.patch(`/api/ops/exits/${tag}`, { data: { disabled: true } })).status()).toBe(200)
      expect((await api.patch(`/api/ops/exits/${tag}`, { data: { disabled: true } })).status()).toBe(200)
    }
    finally {
      expect((await api.patch(`/api/ops/exits/${tag}`, { data: { disabled: false } })).status()).toBe(200)
    }
    const rows = await since(api, mark, r => r.type.startsWith('exit_'))
    const name = tag.toUpperCase()
    expect(rows.map(r => [r.type, r.title, r.detail])).toEqual([
      ['exit_disabled', `Экзит выключен · ${name}`, `вручную · ${ACTOR}`],
      ['exit_enabled', `Экзит включён · ${name}`, `вручную · ${ACTOR}`],
    ])
  })

  test('безопасность: неудачный вход и смена настроек SSO и Telegram — тревоги', async ({ page }) => {
    await loginApi(page)
    const api = page.request
    const mark = await lastId(api)

    // Несуществующий логин: ограничитель попыток admin не задевается.
    const login = uniq('nobody')
    const wrong = await authPost(api, '/sign-in/username', { username: login, password: 'wrong-password-e2e' })
    expect(wrong.status()).toBe(401)

    const sso = await (await api.get('/api/admin/sso')).json()
    const ssoBody = {
      enabled: sso.enabled, authentikUrl: sso.authentikUrl, appSlug: sso.appSlug, clientId: sso.clientId, clientSecret: '',
      allowedSubs: sso.allowedSubs, label: sso.label, autoRedirect: sso.autoRedirect, passwordLogin: sso.passwordLogin, redirectUrl: sso.redirectUrl,
    }
    const tg = await (await api.get('/api/admin/telegram')).json()
    try {
      expect((await api.put('/api/admin/sso', { data: { ...ssoBody, label: 'E2E журнал' } })).status()).toBe(200)
      // Сохранение без изменений — не событие.
      expect((await api.put('/api/admin/telegram', { data: { admin_username: tg.admin_username } })).status()).toBe(200)
      expect((await api.put('/api/admin/telegram', { data: { admin_username: `e2e_${Date.now().toString(36)}` } })).status()).toBe(200)
    }
    finally {
      await api.put('/api/admin/sso', { data: ssoBody })
      await api.put('/api/admin/telegram', { data: { admin_username: tg.admin_username } })
    }

    const security = ['login_failed', 'sso_changed', 'telegram_changed']
    const rows = await since(api, mark, r => security.includes(r.type))
    const ssoDetail = `вход через Authentik ${sso.enabled ? 'включён' : 'выключен'} · ${ACTOR}`
    expect(rows.map(r => [r.type, r.level, r.title, r.detail])).toEqual([
      ['login_failed', 'alarm', 'Неудачный вход в панель', expect.stringMatching(new RegExp(`^логин «${login}» · \\S+$`))],
      ['sso_changed', 'alarm', 'Изменены настройки SSO', ssoDetail],
      ['telegram_changed', 'alarm', 'Изменены настройки Telegram', `ник админа · ${ACTOR}`],
      // Возврат прежних настроек — тоже изменение.
      ['sso_changed', 'alarm', 'Изменены настройки SSO', ssoDetail],
      ['telegram_changed', 'alarm', 'Изменены настройки Telegram', `ник админа · ${ACTOR}`],
    ])
    // Ни секретов, ни пароля в тексте записи.
    expect(JSON.stringify(rows)).not.toContain('wrong-password-e2e')
  })

  test('бот и бэкап сообщают о сбоях только с loopback и с секретом', async ({ page }) => {
    await loginApi(page)
    const api = page.request
    // Снаружи (через Caddy) ручка бота закрыта даже для вошедшего админа.
    const outside = await api.post('/api/bot/events', { data: { type: 'backup_failed', detail: 'снаружи' } })
    expect(outside.status()).toBeGreaterThanOrEqual(401)
    expect(outside.status()).toBeLessThan(500)
    test.skip(!container, 'loopback панели есть только в одноразовом контейнере run-local.sh')

    const mark = await lastId(api)
    const post = (body: object, secret = true) => JSON.parse(inside(`
      const r = await fetch('http://127.0.0.1:31821/api/bot/events', {
        method: 'POST', body: process.argv[1],
        headers: { 'content-type': 'application/json', ...(JSON.parse(process.argv[2]) ? { authorization: 'Bearer ' + process.env.NUXT_TGBOT_SECRET } : {}) },
      }); console.log(r.status);`, body, secret)) as number
    expect(post({ type: 'backup_failed', detail: 'без секрета' }, false)).toBe(401)
    expect(post({ type: 'unknown' })).toBe(400)
    expect(post({ type: 'telegram_rejected', status: 403, recipient: 'client' })).toBe(200)
    expect(post({ type: 'telegram_rejected', status: 400, recipient: 'admin' })).toBe(200)
    expect(post({ type: 'backup_failed', detail: 'нет пароля шифрования' })).toBe(200)

    const rows = await since(api, mark)
    expect(rows.map(r => [r.type, r.level, r.title, r.detail])).toEqual([
      ['telegram_rejected', 'alarm', 'Telegram не принял сообщение бота', 'клиенту · код 403'],
      ['telegram_rejected', 'alarm', 'Telegram не принял сообщение бота', 'админу · код 400'],
      ['backup_failed', 'alarm', 'Бэкап не удался', 'нет пароля шифрования'],
    ])
  })

  test('лента: только для вошедшего, по умолчанию четыре последних', async ({ page, playwright, baseURL }) => {
    const anon = await playwright.request.newContext({ baseURL })
    expect((await anon.get('/api/events')).status()).toBe(401)
    await anon.dispose()
    await loginApi(page)
    const short = await (await page.request.get('/api/events')).json() as FeedRow[]
    expect(short.length).toBeLessThanOrEqual(4)
    const full = await feed(page.request)
    expect(short).toEqual(full.slice(0, short.length))
    expect((await page.request.get('/api/events?limit=0')).status()).toBe(400)
  })
})
