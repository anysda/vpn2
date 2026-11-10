import { type APIRequestContext, test as baseTest } from '@playwright/test'
import { ADMIN_USER, NODES, authPost, expect, loginApi, uniq, test } from './support/fixtures'
import { container, inside } from './support/openvpn-ca'
import { waitAccess } from './support/access'
import * as mskDay from '../server/utils/msk-day'
import * as outbounds from '../shared/outbounds'
import { serverModule } from './support/server-module'

test.describe('events', () => {
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

    test('бот и бэкап сообщают о сбоях и предупреждениях только с loopback и с секретом', async ({ page }) => {
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
      const warning = 'БД и конфиги VPN расходятся (устройства: 7) — перевыпусти в панели'
      expect(post({ type: 'backup_warning', detail: warning })).toBe(200)

      const rows = await since(api, mark)
      expect(rows.map(r => [r.type, r.level, r.title, r.detail])).toEqual([
        ['telegram_rejected', 'alarm', 'Telegram не принял сообщение бота', 'клиенту · код 403'],
        ['telegram_rejected', 'alarm', 'Telegram не принял сообщение бота', 'админу · код 400'],
        ['backup_failed', 'alarm', 'Бэкап не удался', 'нет пароля шифрования'],
        ['backup_warning', 'warn', 'Бэкап сохранён с предупреждением', warning],
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
})

test.describe('events-nodes', () => {
  const test = baseTest

  type Row = { type: string, level: string, object?: string | null, title: string, detail?: string, at?: number }
  type Watch = typeof import('../server/utils/node-watch')

  const INSTANCE = '192.0.2.2:9100'

  /** Фейковая VictoriaMetrics: значения на ноду задаёт тест, down — VM недоступна. */
  class FakeVm {
    down = false
    stale: number | null = 5
    iowait: number | null = 1
    uptime: number | null = 100_000

    fetch = async (_url: string, opts: { query: { query: string } }) => {
      if (this.down) throw new Error('connect ECONNREFUSED')
      const q = opts.query.query
      const value = q.includes('timestamp(node_time_seconds)') ? this.stale
        : q.includes('iowait') ? this.iowait
          : q.includes('node_boot_time_seconds') ? this.uptime
            : null
      return {
        status: 'success',
        data: { resultType: 'vector', result: value === null ? [] : [{ metric: { instance: INSTANCE }, value: [0, String(value)] }] },
      }
    }
  }

  function setup(seed: Row[] = [], foreignBest: { now: string | undefined } = { now: undefined }) {
    const vm = new FakeVm()
    const journal: Row[] = [...seed]
    const metrics = serverModule<typeof import('../server/utils/vm-client')>(
      new URL('../server/utils/vm-client.ts', import.meta.url), {
        './logger': { useLogger: () => ({ warn: () => {} }) },
        './msk-day': mskDay,
      }, {
        useRuntimeConfig: () => ({ vmUrl: 'http://metrics.invalid', mgmtIps: 'de:192.0.2.2', exitTags: 'de' }),
        $fetch: (url: string, opts: { query: { query: string } }) => vm.fetch(url, opts),
      },
    )
    // Только экзит de: ru в mgmtIps не задан, его инстанс в VM пуст — фильтруем.
    const only = { ...metrics, nodeInstances: () => metrics.nodeInstances().filter(n => n.tag === 'de') }
    const watch = serverModule<Watch>(new URL('../server/utils/node-watch.ts', import.meta.url), {
      './vm-client': only,
      './events': {
        recordEvent: async (row: Row) => { journal.push(row); return true },
        lastEventOf: async (object: string, types: string[]) => {
          const hit = [...journal].reverse().find(r => r.object === object && types.includes(r.type))
          return hit ? { type: hit.type, at: hit.at ?? 0, detail: hit.detail ?? '' } : null
        },
      },
      './clash-client': { fetchProxies: async () => [{ name: 'foreign-best', type: 'Selector', now: foreignBest.now }] },
      './exit-control': { exitNodeOf: (o: string | undefined) => (o ? outbounds.parseOutbound(o)?.tag ?? null : null) },
    })
    return { vm, journal, watch }
  }

  const MIN = 60_000

  test('нода: оффлайн и возврат — по одной записи на переход, с простоем', async () => {
    const { vm, journal, watch } = setup()
    let t = 1_000_000_000_000
    await watch.checkNodes(t)
    expect(journal).toHaveLength(0)

    vm.stale = 400
    for (let i = 0; i < 4; i++) await watch.checkNodes(t += 30_000)
    expect(journal.map(r => r.type)).toEqual(['node_offline'])
    expect(journal[0]).toMatchObject({ level: 'alarm', object: 'node:de', title: 'DE оффлайн' })

    const downAt = journal[0]!.at!
    vm.stale = 2
    vm.uptime = 3 * 60
    t = downAt + 4 * MIN
    await watch.checkNodes(t)
    await watch.checkNodes(t + 30_000)
    expect(journal.map(r => r.type)).toEqual(['node_offline', 'node_online'])
    // Последняя метрика за 400 с до «оффлайн»: 6 мин 40 с + 4 мин.
    expect(journal[1]).toMatchObject({ level: 'ok', title: 'DE снова в сети', detail: 'после перезагрузки · простой 10 мин' })
  })

  test('нода: пропавшая серия — оффлайн только со второй проверки, одиночный провал не пишется', async () => {
    const { vm, journal, watch } = setup()
    let t = 2_000_000_000_000
    vm.stale = null
    await watch.checkNodes(t += 30_000)
    vm.stale = 3
    await watch.checkNodes(t += 30_000)
    expect(journal).toHaveLength(0)

    vm.stale = null
    await watch.checkNodes(t += 30_000)
    expect(journal).toHaveLength(0)
    await watch.checkNodes(t += 30_000)
    expect(journal.map(r => r.type)).toEqual(['node_offline'])

    // Возврат без перезагрузки: аптайм больше простоя.
    vm.stale = 1
    vm.uptime = 10 * 86400
    await watch.checkNodes(t + 2 * MIN)
    // Последняя метрика за 3 с до первого провала, за минуту до «оффлайн».
    expect(journal[1]).toMatchObject({ type: 'node_online', detail: 'простой 3 мин' })
  })

  test('нода: отставание метрик больше 60 с — одна тревога, без повторов', async () => {
    const { vm, journal, watch } = setup()
    let t = 3_000_000_000_000
    vm.stale = 59
    await watch.checkNodes(t += 30_000)
    expect(journal).toHaveLength(0)
    vm.stale = 75
    await watch.checkNodes(t += 30_000)
    vm.stale = 105
    await watch.checkNodes(t + 30_000)
    expect(journal).toHaveLength(1)
    expect(journal[0]).toMatchObject({ type: 'node_lag', level: 'alarm', title: 'DE: метрики отстают', detail: 'нет данных 75 с · проверка каждые 30 с' })
  })

  test('нода: iowait выше 30% две проверки подряд — одна тревога на эпизод', async () => {
    const { vm, journal, watch } = setup()
    let t = 4_000_000_000_000
    vm.iowait = 34
    await watch.checkNodes(t += 30_000)
    vm.iowait = 5
    await watch.checkNodes(t += 30_000)
    vm.iowait = 34
    await watch.checkNodes(t += 30_000)
    expect(journal).toHaveLength(0)
    await watch.checkNodes(t += 30_000)
    await watch.checkNodes(t += 30_000)
    expect(journal).toEqual([expect.objectContaining({ type: 'node_iowait', detail: 'iowait 34% · проверка каждые 30 с' })])

    // Новый эпизод после спада — новая запись.
    vm.iowait = 10
    await watch.checkNodes(t += 30_000)
    vm.iowait = 40
    await watch.checkNodes(t += 30_000)
    await watch.checkNodes(t + 30_000)
    expect(journal.filter(r => r.type === 'node_iowait')).toHaveLength(2)
  })

  test('нода: недоступная VictoriaMetrics — не «все ноды оффлайн»', async () => {
    const { vm, journal, watch } = setup()
    vm.down = true
    for (let i = 0; i < 3; i++) {
      // Промис из чужого vm-контекста: матчер rejects его не узнаёт.
      const failed = await watch.checkNodes(5_000_000_000_000 + i * 30_000).then(() => false, () => true)
      expect(failed).toBe(true)
    }
    expect(journal).toHaveLength(0)
  })

  test('нода: рестарт панели не повторяет «оффлайн» и считает простой с начала', async () => {
    const downAt = 6_000_000_000_000
    const { vm, journal, watch } = setup([{ type: 'node_offline', level: 'alarm', object: 'node:de', title: 'DE оффлайн', at: downAt }])
    vm.stale = 500
    await watch.checkNodes(downAt + 10 * MIN)
    await watch.checkNodes(downAt + 11 * MIN)
    expect(journal).toHaveLength(1)
    vm.stale = 1
    await watch.checkNodes(downAt + 20 * MIN)
    expect(journal[1]).toMatchObject({ type: 'node_online', detail: 'простой 20 мин' })
  })

  test('нода: «отстают» → «оффлайн» → в сети — простой с первой потери метрик', async () => {
    const { vm, journal, watch } = setup()
    vm.uptime = 10 * 86400
    let t = 7_000_000_000_000
    await watch.checkNodes(t)
    vm.stale = 75
    await watch.checkNodes(t += 30_000)
    const lagAt = t
    vm.stale = 195
    await watch.checkNodes(t + 2 * MIN)
    expect(journal.map(r => r.type)).toEqual(['node_lag', 'node_offline'])
    vm.stale = 1
    await watch.checkNodes(lagAt + 4 * MIN + 10_000)
    // От последней метрики: 75 с до «отстают» + 4 мин 10 с.
    expect(journal[2]).toMatchObject({ type: 'node_online', title: 'DE снова в сети', detail: 'простой 5 мин' })
  })

  test('нода: «отстают» → в сети, потом «оффлайн» напрямую — давнее «отстают» к простою не относится', async () => {
    const { vm, journal, watch } = setup()
    vm.uptime = 10 * 86400
    let t = 8_000_000_000_000
    vm.stale = 75
    await watch.checkNodes(t += 30_000)
    vm.stale = 2
    await watch.checkNodes(t += 30_000)
    // Отставание без «оффлайн» — возврат в ленту не пишется.
    expect(journal.map(r => r.type)).toEqual(['node_lag'])

    vm.stale = 400
    await watch.checkNodes(t += 30_000)
    const downAt = t
    vm.stale = 1
    await watch.checkNodes(downAt + 3 * MIN)
    expect(journal.map(r => r.type)).toEqual(['node_lag', 'node_offline', 'node_online'])
    // Последняя метрика за 400 с до «оффлайн», а не с давнего «отстают».
    expect(journal[2]).toMatchObject({ detail: 'простой 9 мин' })
  })

  test('нода: рестарт панели после «отстают» → «оффлайн» — простой с «отстают»', async () => {
    const lagAt = 9_000_000_000_000
    const { vm, journal, watch } = setup([
      { type: 'node_lag', level: 'alarm', object: 'node:de', title: 'DE: метрики отстают', at: lagAt },
      { type: 'node_offline', level: 'alarm', object: 'node:de', title: 'DE оффлайн', at: lagAt + 2 * MIN },
    ])
    vm.uptime = 10 * 86400
    vm.stale = 1
    await watch.checkNodes(lagAt + 6 * MIN)
    expect(journal[2]).toMatchObject({ type: 'node_online', detail: 'простой 6 мин' })
  })

  test('нода: рестарт панели — давнее «отстают» перед «оффлайн» к простою не относится', async () => {
    const lagAt = 9_500_000_000_000
    const downAt = lagAt + 60 * MIN
    const { vm, journal, watch } = setup([
      { type: 'node_lag', level: 'alarm', object: 'node:de', title: 'DE: метрики отстают', at: lagAt },
      { type: 'node_offline', level: 'alarm', object: 'node:de', title: 'DE оффлайн', at: downAt },
    ])
    vm.uptime = 10 * 86400
    vm.stale = 1
    await watch.checkNodes(downAt + 5 * MIN)
    expect(journal[2]).toMatchObject({ type: 'node_online', detail: 'простой 5 мин' })
  })

  // Стенд: метрики закрыты 4 мин 10 с, «отстают» заметили с «нет данных 83 с»,
  // возврат через 3 мин после «отстают». Простой — от последней метрики.
  test('нода: «отстают» (нет данных 83 с) → «оффлайн» → в сети — простой от последней метрики', async () => {
    const { vm, journal, watch } = setup()
    vm.uptime = 10 * 86400
    const lastMetric = 10_000_000_000_000
    // Проверка каждые 30 с, возраст последней метрики растёт вместе со временем.
    for (const stale of [23, 53, 83, 113, 143, 173, 203]) {
      vm.stale = stale
      await watch.checkNodes(lastMetric + stale * 1000)
    }
    expect(journal.map(r => r.type)).toEqual(['node_lag', 'node_offline'])
    expect(journal[0]).toMatchObject({ detail: 'нет данных 83 с · проверка каждые 30 с' })
    vm.stale = 233
    await watch.checkNodes(lastMetric + 233_000)
    // Метрики вернулись через 4 мин 10 с, проверка заметила через 13 с.
    vm.stale = 3
    await watch.checkNodes(lastMetric + 263_000)
    expect(journal[2]).toMatchObject({ type: 'node_online', detail: 'простой 4 мин' })
  })

  test('нода: метрики пропали целиком — «оффлайн» без «отстают», простой от последней метрики', async () => {
    const { vm, journal, watch } = setup()
    vm.uptime = 10 * 86400
    const t = 11_000_000_000_000
    vm.stale = 10
    await watch.checkNodes(t)
    const lastMetric = t - 10_000
    vm.stale = null
    await watch.checkNodes(t + 30_000)
    await watch.checkNodes(t + 60_000)
    expect(journal.map(r => r.type)).toEqual(['node_offline'])
    vm.stale = 2
    await watch.checkNodes(lastMetric + 4 * MIN + 10_000)
    expect(journal[1]).toMatchObject({ type: 'node_online', detail: 'простой 4 мин' })
  })

  test('нода: рестарт панели после «отстают» (нет данных 83 с) → «оффлайн» — простой от последней метрики', async () => {
    const lastMetric = 12_000_000_000_000
    const lagAt = lastMetric + 83_000
    const { vm, journal, watch } = setup([
      { type: 'node_lag', level: 'alarm', object: 'node:de', title: 'DE: метрики отстают', detail: 'нет данных 83 с · проверка каждые 30 с', at: lagAt },
      { type: 'node_offline', level: 'alarm', object: 'node:de', title: 'DE оффлайн', detail: 'нет метрик · проверка каждые 30 с', at: lagAt + 2 * MIN },
    ])
    vm.uptime = 10 * 86400
    vm.stale = 3
    await watch.checkNodes(lastMetric + 263_000)
    expect(journal[2]).toMatchObject({ type: 'node_online', detail: 'простой 4 мин' })
  })

  test('нода: рестарт панели на «отстают», дальше «оффлайн» — простой от последней метрики', async () => {
    const lastMetric = 13_000_000_000_000
    const lagAt = lastMetric + 83_000
    const { vm, journal, watch } = setup([
      { type: 'node_lag', level: 'alarm', object: 'node:de', title: 'DE: метрики отстают', detail: 'нет данных 83 с · проверка каждые 30 с', at: lagAt },
    ])
    vm.uptime = 10 * 86400
    vm.stale = 203
    await watch.checkNodes(lastMetric + 203_000)
    vm.stale = 3
    await watch.checkNodes(lastMetric + 263_000)
    expect(journal.map(r => r.type)).toEqual(['node_lag', 'node_offline', 'node_online'])
    expect(journal[2]).toMatchObject({ detail: 'простой 4 мин' })
  })

  test('failover: сторож увёл foreign-best на другой узел — тревога', async () => {
    const proxy = { now: 'hy2-de-direct' as string | undefined }
    const { journal, watch } = setup([], proxy)
    await watch.checkFailover(1)
    await watch.checkFailover(2)
    expect(journal).toHaveLength(0)
    proxy.now = 'hy2-nl-warp'
    await watch.checkFailover(3)
    // Clash недоступен — не переход.
    proxy.now = undefined
    await watch.checkFailover(4)
    proxy.now = 'hy2-nl-direct'
    await watch.checkFailover(5)
    expect(journal).toEqual([expect.objectContaining({ type: 'failover', level: 'alarm', title: 'Сторож переключил экзит', detail: 'DE → NL' })])
  })
})
