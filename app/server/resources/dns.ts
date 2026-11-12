import { domainToASCII } from 'node:url'
import { z } from 'zod'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'

const CheckQuery = z.object({ name: z.string().trim().toLowerCase().transform(s => domainToASCII(s.replace(/\.$/, '')) || s).pipe(z.string().regex(/^[a-z0-9_*.-]+$/).max(253)) })
const LogQuery = z.object({
  f: z.enum(['all', 'blk', 'ok', 'rw']).default('all'),
  q: z.string().trim().max(200).default(''),
  older: z.string().max(64).optional(),
})
const PAGE = 500
const SCAN = 10
const WANT = 100

const LogRule = z.object({
  domain: z.string().trim().toLowerCase().regex(/^[a-z0-9_-]+(\.[a-z0-9_-]+)*\.?$/).max(253),
  block: z.boolean(),
  client: z.string().max(64).optional(),
})
const List = z.object({ url: z.string().min(1).max(2048), white: z.boolean() })
const NewList = z.object({
  name: z.string().trim().min(1).max(120),
  url: z.string().trim().regex(/^https?:\/\/\S+$/i).max(2048),
  white: z.boolean(),
})
const Rewrite = z.object({ domain: z.string().min(1).max(253), answer: z.string().min(1).max(253) })

const HOUR = 3_600_000
const LOG_HOURS = [24, 168, 2160]
const STATS_HOURS = [24, 168, 720]
// Одно поле за раз, как в макете: переключатель, сегмент или «Изменить».
const Setting = z.union([
  z.object({ upstreams: z.string().max(10_000) }),
  z.object({ mode: z.enum(['load_balance', 'parallel', 'fastest_addr']) }),
  z.object({ bootstrap: z.string().max(2000) }),
  z.object({ safebrowsing: z.boolean() }),
  z.object({ parental: z.boolean() }),
  z.object({ safesearch: z.boolean() }),
  z.object({ dnssec: z.boolean() }),
  z.object({ noAaaa: z.boolean() }),
  z.object({ optimistic: z.boolean() }),
  z.object({ ratelimit: z.string().max(100) }),
  z.object({ block: z.string().max(100) }),
  z.object({ logHours: z.number().int() }),
  z.object({ statsHours: z.number().int() }),
  z.object({ anonymize: z.boolean() }),
])
const badSetting = (message: string) => createError({ statusCode: 422, statusMessage: 'bad_setting', message })
const userRules = (st: { user_rules?: string[] | null }) => (st.user_rules ?? []).join('\n')
const post = (path: string, body?: Record<string, unknown>) => agh(path, { method: 'POST', body })
const put = (path: string, body: Record<string, unknown>) => agh(path, { method: 'PUT', body })
const ok = () => ({ ok: true })
const filtersConfirmed = async (good: (st: Awaited<ReturnType<typeof filteringStatus>>) => boolean, what: string) =>
  confirmAgh(await filteringStatus(), good, what)
const rewritesConfirmed = async (good: (l: Awaited<ReturnType<typeof rewriteList>>) => boolean, what: string) =>
  confirmAgh(await rewriteList(), good, what)

type State = Awaited<ReturnType<typeof settingsState>>
const dnsConfig = (body: Record<string, unknown>) => post('dns_config', body)
const toggle = (path: string) => (on: boolean) => post(`${path}/${on ? 'enable' : 'disable'}`)
const upstreamList = (text: string) => {
  const list = upstreamLines(text)
  if (!list.length) throw badSetting('Нужен хотя бы один сервер выше')
  return list
}

/** Как применить каждую настройку DNS в AdGuard; значение уже проверено схемой `Setting`. */
const APPLY: Record<string, (value: never, cur: State) => Promise<unknown>> = {
  upstreams: async (text: string) => {
    try {
      await dnsConfig({ upstream_dns: upstreamList(text) })
    }
    catch (e) {
      const x = e as { statusMessage?: string, message?: string }
      if (x.statusMessage !== 'agh_rejected') throw e
      throw badSetting(upstreamError(x.message ?? ''))
    }
  },
  mode: (mode: string) => dnsConfig({ upstream_mode: mode }),
  bootstrap: (text: string) => {
    const r = parseBootstrap(text)
    if ('error' in r) throw badSetting(r.error)
    return dnsConfig({ bootstrap_dns: r.list })
  },
  safebrowsing: toggle('safebrowsing'),
  parental: toggle('parental'),
  safesearch: (on: boolean, cur) => put('safesearch/settings', { ...cur.ss, enabled: on }),
  dnssec: (on: boolean) => dnsConfig({ dnssec_enabled: on }),
  noAaaa: (on: boolean) => dnsConfig({ disable_ipv6: on }),
  optimistic: (on: boolean) => dnsConfig({ cache_optimistic: on }),
  ratelimit: (text: string) => {
    const n = parseRatelimit(text)
    if (n === null) throw badSetting('Лимит запросов: число в секунду или «без лимита»')
    return dnsConfig({ ratelimit: n })
  },
  block: async (text: string, cur) => {
    const r = parseBlock(text, cur.info)
    if ('error' in r) throw badSetting(r.error)
    if (r.set) await dnsConfig(r.set as Record<string, unknown>)
  },
  logHours: (hours: number, cur) => {
    if (!LOG_HOURS.includes(hours)) throw badSetting('Журнал хранится 1, 7 или 90 дней')
    return put('querylog/config/update', { ...cur.log, interval: hours * HOUR })
  },
  statsHours: (hours: number, cur) => {
    if (!STATS_HOURS.includes(hours)) throw badSetting('Статистика хранится 24 часа, 7 или 30 дней')
    return put('stats/config/update', { ...cur.stats, interval: hours * HOUR })
  },
  anonymize: (on: boolean, cur) => put('querylog/config/update', { ...cur.log, anonymize_client_ip: on }),
}

/** Что панель ждёт увидеть в настройках после записи; undefined - не сверяется. */
const EXPECT: Record<string, (value: never) => unknown> = {
  upstreams: (text: string) => upstreamLines(text).join('\n'),
  bootstrap: (text: string) => (parseBootstrap(text) as { list: string[] }).list.join(', '),
  ratelimit: () => undefined,
  block: () => undefined,
}

export default {
  'GET /dns/summary': admin(async () => {
    const [stats, info, log, names] = await Promise.all([
      agh<AghStats>('stats'),
      agh<{ upstream_mode: string }>('dns_info'),
      agh<{ data: AghLogEntry[] | null }>('querylog', { query: { limit: 1000 } }),
      panelNames(),
    ])
    return summarize(stats, names, info.upstream_mode, log.data ?? [])
  }),

  'GET /dns/check': admin(async (event) => {
    const q = CheckQuery.safeParse(getQuery(event))
    if (!q.success) throw createError({ statusCode: 400, statusMessage: 'bad_domain', message: 'Это не похоже на домен' })
    const [r, st, services] = await Promise.all([
      agh<CheckHost>('filtering/check_host', { query: { name: q.data.name } }),
      filteringStatus(),
      serviceNames(),
    ])
    return verdict(r, listNames(st), services)
  }),

  'GET /dns/clients': admin(async () => {
    const db = useDb()
    const [cl, dv, aghClients, stats, names] = await Promise.all([
      db.select().from(clients).orderBy(...clientsNewestFirst),
      db.select({ clientId: devices.clientId, wg: devices.wgIp, ovpn: devices.ovpnIp, ikev2: devices.ikev2Ip }).from(devices),
      agh<{ clients: AghPersistentClient[] | null }>('clients'),
      agh<AghStats>('stats'),
      serviceNames(),
    ])
    const panel = cl.map(c => ({
      name: c.name,
      tg: c.tgUsername,
      filter: c.filterTraffic,
      ips: dv.filter(d => d.clientId === c.id).flatMap(d => [d.wg, d.ovpn, d.ikev2]).filter((x): x is string => !!x),
    }))
    const counts = new Map((stats.top_clients ?? []).flatMap(o => Object.entries(o)))
    return clientRows(panel, aghClients.clients ?? [], counts, names)
  }),

  /** Поиск идёт и по именам клиентов панели: страницы дочитываются до WANT строк или SCAN страниц, `older` продолжает. */
  'GET /dns/log': admin(async (event) => {
    const p = LogQuery.safeParse(getQuery(event))
    if (!p.success) throw createError({ statusCode: 400, statusMessage: 'invalid_query', message: 'Проверьте фильтр журнала' })
    const { f, q } = p.data
    const [names, st, services] = await Promise.all([panelNames(), filteringStatus(), serviceNames()])
    const lists = listNames(st)
    const byName = q && [...names.values()].some(n => n.toLowerCase().includes(q.toLowerCase()))
    const rows: Array<ReturnType<typeof logRow>> = []
    let older = p.data.older
    for (let i = 0; i < SCAN && rows.length < WANT; i++) {
      const page = await agh<{ data: AghLogEntry[] | null, oldest: string }>('querylog', {
        query: { limit: PAGE, older_than: older, response_status: f === 'blk' || f === 'rw' ? 'filtered' : 'all', search: q && !byName ? q : undefined },
      })
      const data = page.data ?? []
      rows.push(...data.map(e => logRow(e, names, lists, services)).filter(r => logMatch(r, f, q)))
      older = data.length < PAGE || !page.oldest ? undefined : page.oldest
      if (!older) break
    }
    return { rows, older: older ?? null }
  }),

  /**
   * «Заблокировать» / «Разблокировать» из строки журнала. Сначала снимается противоположное
   * точное правило, новое добавляется, только если без него AdGuard отвечает по-прежнему.
   * Не помогло и оно (домен держит блокировка сервиса) - свои правила возвращаются как были.
   */
  'POST /dns/log-rule': admin(async (event) => {
    const { domain, block, client } = await readBodyAs(event, LogRule)
    const d = domain.replace(/\.$/, '')
    const before = (await filteringStatus()).user_rules ?? []
    const set = (rules: string[]) => post('filtering/set_rules', { rules })
    const done = (r: { reason: string }) => r.reason.startsWith('Filtered') === block
    const t = toggleRules(before, d, block)
    const gone = before.filter(r => !t.rules.includes(r))
    if (t.removed) await set(t.rules)
    let r = await checkUntil(d, client, x => !citesGone(x, gone))
    if (!done(r)) {
      await set([...t.rules, t.rule])
      r = await checkUntil(d, client, x => done(x) || (!block && x.reason === 'FilteredBlockedService'))
    }
    if (done(r)) return ok()
    await set(before)
    const why = r.reason === 'FilteredBlockedService'
      ? `его блокирует сервис «${(await serviceNames()).get(r.service_name ?? '') ?? r.service_name}»`
      : `AdGuard отвечает ${r.reason}`
    throw createError({ statusCode: 409, statusMessage: 'rule_not_enough', message: `Правило для ${d} не помогло: ${why}` })
  }),

  'GET /dns/rules': admin(async () => ({ text: userRules(await filteringStatus()) })),
  'PUT /dns/rules': admin(async (event) => {
    const lines = (await readBodyAs(event, z.object({ text: z.string().max(1_000_000) }))).text.replace(/\r/g, '').split('\n')
    while (lines.length && !lines.at(-1)!.trim()) lines.pop()
    const errors = checkRules(lines)
    if (errors.length) {
      const message = `Строка ${errors[0]!.line}: ${errors[0]!.why}${errors.length > 1 ? ` (и ещё ${errors.length - 1})` : ''}`
      throw createError({ statusCode: 422, statusMessage: 'bad_rules', message, data: { message, errors } })
    }
    await post('filtering/set_rules', { rules: lines })
    return { text: userRules(await filtersConfirmed(st => userRules(st) === lines.join('\n'), 'свои правила не сохранились')) }
  }),

  'GET /dns/filters': admin(async () => listsView(await filteringStatus())),
  /** AdGuard сам скачивает список и отказывает, если не смог. */
  'POST /dns/filters': admin(async (event) => {
    const { name, url, white } = await readBodyAs(event, NewList)
    await agh('filtering/add_url', { method: 'POST', body: { name, url, whitelist: white }, timeout: 60_000 })
    return listsView(await filtersConfirmed(st => !!findList(st, url, white), `список «${name}» не появился`))
  }),
  'PATCH /dns/filters': admin(async (event) => {
    const { url, white, enabled } = await readBodyAs(event, List.extend({ enabled: z.boolean() }))
    const f = findList(await filteringStatus(), url, white)
    if (!f) throw createError({ statusCode: 404, statusMessage: 'list_not_found', message: 'Такого списка уже нет в AdGuard' })
    await agh('filtering/set_url', { method: 'POST', body: { url, whitelist: white, data: { name: f.name, url, enabled } }, timeout: 60_000 })
    return listsView(await filtersConfirmed(st => findList(st, url, white)?.enabled === enabled, `список «${f.name}» ${enabled ? 'не включился' : 'не выключился'}`))
  }),
  'DELETE /dns/filters': admin(async (event) => {
    const { url, white } = await readBodyAs(event, List)
    await post('filtering/remove_url', { url, whitelist: white })
    return listsView(await filtersConfirmed(st => !findList(st, url, white), 'список остался'))
  }),
  /** «Проверить обновления»: AdGuard перекачивает включённые списки, чёрные и белые. */
  'POST /dns/filters/refresh': admin(async () => {
    let updated = 0
    for (const whitelist of [false, true]) {
      updated += (await agh<{ updated: number }>('filtering/refresh', { method: 'POST', body: { whitelist }, timeout: 120_000 })).updated ?? 0
    }
    return { updated, ...listsView(await filteringStatus()) }
  }),

  'GET /dns/rewrites': admin(() => rewriteList()),
  'POST /dns/rewrites': admin(async (event) => {
    const b = await readBodyAs(event, z.object({ domain: z.string().max(253), answer: z.string().max(253) }))
    const rw = { domain: b.domain.trim().toLowerCase().replace(/\.$/, ''), answer: b.answer.trim() }
    const bad = checkRewrite(rw.domain, rw.answer)
    if (bad) throw createError({ statusCode: 422, statusMessage: 'bad_rewrite', message: bad })
    if ((await rewriteList()).some(x => sameRewrite(x, rw))) {
      throw createError({ statusCode: 409, statusMessage: 'rewrite_exists', message: `Перезапись «${rw.domain}» → ${rw.answer} уже есть` })
    }
    await post('rewrite/add', { ...rw, enabled: true })
    return rewritesConfirmed(l => l.some(x => sameRewrite(x, rw)), `перезапись «${rw.domain}» не появилась`)
  }),
  'PATCH /dns/rewrites': admin(async (event) => {
    const { domain, answer, enabled } = await readBodyAs(event, Rewrite.extend({ enabled: z.boolean() }))
    const cur = await findRewrite(domain, answer)
    await put('rewrite/update', { target: cur, update: { domain, answer, enabled } })
    return rewritesConfirmed(l => l.some(x => sameRewrite(x, cur) && x.enabled === enabled), `перезапись «${domain}» ${enabled ? 'не включилась' : 'не выключилась'}`)
  }),
  'DELETE /dns/rewrites': admin(async (event) => {
    const { domain, answer } = await readBodyAs(event, Rewrite)
    const cur = await findRewrite(domain, answer)
    await post('rewrite/delete', { domain: cur.domain, answer: cur.answer })
    return rewritesConfirmed(l => !l.some(x => sameRewrite(x, cur)), `перезапись «${domain}» не удалилась`)
  }),

  /** Заблокированные для всех клиентов; расписание AdGuard не трогаем. */
  'GET /dns/services': admin(async () => {
    const [all, blocked] = await Promise.all([serviceCatalog(), blockedServices()])
    return { all: all.map(s => ({ id: s.id, name: s.name })), blocked: blocked.ids ?? [] }
  }),
  'PUT /dns/services': admin(async (event) => {
    const ids = [...new Set((await readBodyAs(event, z.object({ ids: z.array(z.string().max(64)).max(1000) }))).ids)]
    const known = new Set((await serviceCatalog()).map(s => s.id))
    const unknown = ids.find(id => !known.has(id))
    if (unknown) throw createError({ statusCode: 422, statusMessage: 'unknown_service', message: `Сервиса «${unknown}» нет в AdGuard` })
    await put('blocked_services/update', { ids, schedule: (await blockedServices()).schedule })
    const now = (await blockedServices()).ids ?? []
    if (now.length !== ids.length || ids.some(id => !now.includes(id))) {
      throw createError({ statusCode: 502, statusMessage: 'agh_not_applied', message: 'AdGuard не применил изменение: блокировка сервисов' })
    }
    return { blocked: now }
  }),

  'GET /dns/settings': admin(async () => settingsView(await settingsState())),
  /** Одна настройка DNS; ответ - что AdGuard показывает после. */
  'PATCH /dns/settings': admin(async (event) => {
    const [key, value] = Object.entries(await readBodyAs(event, Setting))[0] as [string, never]
    await APPLY[key]!(value, await settingsState())
    const view = settingsView(await settingsState())
    const expected = EXPECT[key] ? EXPECT[key](value) : value
    if (expected !== undefined && view[key as keyof typeof view] !== expected) {
      throw createError({ statusCode: 502, statusMessage: 'agh_not_applied', message: 'AdGuard не применил изменение настройки' })
    }
    return view
  }),
  'POST /dns/settings/cache-clear': admin(async () => {
    await post('cache_clear')
    return ok()
  }),
  'POST /dns/settings/reset': admin(async (event) => {
    const { what } = await readBodyAs(event, z.object({ what: z.enum(['log', 'stats']) }))
    await post(what === 'log' ? 'querylog_clear' : 'stats_reset')
    return ok()
  }),
  'POST /dns/settings/upstreams-check': admin(async (event) => {
    const b = await readBodyAs(event, z.object({ upstreams: z.string().max(10_000), bootstrap: z.string().max(2000) }))
    const list = upstreamList(b.upstreams)
    const boot = parseBootstrap(b.bootstrap)
    const res = await agh<Record<string, string>>('test_upstream_dns', {
      method: 'POST',
      body: { upstream_dns: list, bootstrap_dns: 'list' in boot ? boot.list : [], fallback_dns: [], private_upstream: [] },
      timeout: 20_000,
    })
    return { total: Object.keys(res).length, failed: Object.entries(res).filter(([, v]) => v !== 'OK').map(([k]) => k) }
  }),
}
