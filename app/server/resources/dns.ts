import { domainToASCII } from 'node:url'
import { z } from 'zod'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'
import { agh } from '../utils/agh-client'
import { logMatch, logRow, summarize, toggleRules, type AghLogEntry, type AghStats, type CheckHost, type LogRow } from '../utils/agh-parse'
import { checkRules, verdict } from '../utils/agh-rules'
import { admin } from '../utils/api'
import {
  blockedServices, checkUntil, citesGone, confirmFiltering, confirmRewrites, filteringStatus, findList, findRewrite,
  listNames, listsView, panelNames, rewriteList, serviceCatalog, serviceNames,
} from '../utils/dns-context'
import {
  checkRewrite, clientRows, parseBlock, parseBootstrap, parseRatelimit, sameRewrite, upstreamError, upstreamLines,
  type AghPersistentClient, type PanelClient, type SettingsView,
} from '../utils/dns-settings'
import { settingsState, viewOf } from '../utils/dns-settings-read'
import { clientsNewestFirst } from '../utils/list-order'
import { readBodyAs } from '../utils/validate'

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
// Одно поле за раз — как в макете: переключатель, сегмент или «Изменить».
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

export default {
  /** Сводка вкладки DNS из статистики AdGuard и последних записей журнала. */
  'GET /dns/summary': admin(async () => {
    const [stats, info, log, names] = await Promise.all([
      agh<AghStats>('stats'),
      agh<{ upstream_mode: string }>('dns_info'),
      agh<{ data: AghLogEntry[] | null }>('querylog', { query: { limit: 1000 } }),
      panelNames(),
    ])
    return summarize(stats, names, info.upstream_mode, log.data ?? [])
  }),

  /** «Проверка фильтрации»: что AdGuard ответит на запрос этого домена. */
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

  /** Клиенты панели глазами AdGuard: адреса в туннеле, свои настройки, запросы. */
  'GET /dns/clients': admin(async () => {
    const db = useDb()
    const [cl, dv, aghClients, stats, names] = await Promise.all([
      db.select().from(clients).orderBy(...clientsNewestFirst),
      db.select({ clientId: devices.clientId, wg: devices.wgIp, ovpn: devices.ovpnIp, ikev2: devices.ikev2Ip }).from(devices),
      agh<{ clients: AghPersistentClient[] | null }>('clients'),
      agh<AghStats>('stats'),
      serviceNames(),
    ])
    const panel: PanelClient[] = cl.map(c => ({
      name: c.name,
      tg: c.tgUsername,
      filter: c.filterTraffic,
      ips: dv.filter(d => d.clientId === c.id).flatMap(d => [d.wg, d.ovpn, d.ikev2]).filter((x): x is string => !!x),
    }))
    const counts = new Map((stats.top_clients ?? []).flatMap(o => Object.entries(o)))
    return clientRows(panel, aghClients.clients ?? [], counts, names)
  }),

  /**
   * Журнал запросов. Фильтр и поиск точные: поиск идёт и по именам клиентов
   * панели, которых AdGuard не знает, поэтому страницы AdGuard дочитываются
   * здесь — до WANT строк или SCAN страниц; `older` продолжает с места остановки.
   */
  'GET /dns/log': admin(async (event) => {
    const p = LogQuery.safeParse(getQuery(event))
    if (!p.success) throw createError({ statusCode: 400, statusMessage: 'invalid_query', message: 'Проверьте фильтр журнала' })
    const { f, q } = p.data
    const [names, st, services] = await Promise.all([panelNames(), filteringStatus(), serviceNames()])
    const lists = listNames(st)
    const byName = q && [...names.values()].some(n => n.toLowerCase().includes(q.toLowerCase()))
    const rows: LogRow[] = []
    let older = p.data.older
    for (let i = 0; i < SCAN && rows.length < WANT; i++) {
      const page = await agh<{ data: AghLogEntry[] | null, oldest: string }>('querylog', {
        query: {
          limit: PAGE,
          older_than: older,
          response_status: f === 'blk' || f === 'rw' ? 'filtered' : 'all',
          search: q && !byName ? q : undefined,
        },
      })
      const data = page.data ?? []
      for (const e of data) {
        const r = logRow(e, names, lists, services)
        if (logMatch(r, f, q)) rows.push(r)
      }
      older = data.length < PAGE || !page.oldest ? undefined : page.oldest
      if (!older) break
    }
    return { rows, older: older ?? null }
  }),

  /**
   * «Заблокировать» / «Разблокировать» из строки журнала своим правилом.
   * Сначала снимаем противоположное точное правило; новое добавляем, только если
   * без него AdGuard всё ещё отвечает по-старому. Не помогло и правило (домен
   * держит блокировка сервиса) — свои правила возвращаем как были.
   * Правила AdGuard применяет не сразу, поэтому ответ check_host ждём.
   */
  'POST /dns/log-rule': admin(async (event) => {
    const { domain, block, client } = await readBodyAs(event, LogRule)
    const d = domain.replace(/\.$/, '')
    const before = (await filteringStatus()).user_rules ?? []
    const set = (rules: string[]) => agh('filtering/set_rules', { method: 'POST', body: { rules } })
    const ok = (r: { reason: string }) => r.reason.startsWith('Filtered') === block
    const t = toggleRules(before, d, block)
    const gone = before.filter(r => !t.rules.includes(r))
    if (t.removed) await set(t.rules)
    // Снятое правило ещё в ответе — движок не пересобран, ждём его.
    let r = await checkUntil(d, client, x => !citesGone(x, gone))
    if (!ok(r)) {
      await set([...t.rules, t.rule])
      r = await checkUntil(d, client, x => ok(x) || (!block && x.reason === 'FilteredBlockedService'))
    }
    if (!ok(r)) {
      await set(before)
      const why = r.reason === 'FilteredBlockedService'
        ? `его блокирует сервис «${(await serviceNames()).get(r.service_name ?? '') ?? r.service_name}»`
        : `AdGuard отвечает ${r.reason}`
      throw createError({ statusCode: 409, statusMessage: 'rule_not_enough', message: `Правило для ${d} не помогло: ${why}` })
    }
    return { ok: true }
  }),

  'GET /dns/rules': admin(async () => ({ text: userRules(await filteringStatus()) })),
  /**
   * «Применить» свои правила. Строку с ошибкой AdGuard принял бы и молча
   * пропустил — такой текст не отправляем, человеку номер строки и причина.
   */
  'PUT /dns/rules': admin(async (event) => {
    const lines = (await readBodyAs(event, z.object({ text: z.string().max(1_000_000) }))).text.replace(/\r/g, '').split('\n')
    while (lines.length && !lines.at(-1)!.trim()) lines.pop()
    const errors = checkRules(lines)
    if (errors.length) {
      const e = errors[0]!
      const message = `Строка ${e.line}: ${e.why}${errors.length > 1 ? ` (и ещё ${errors.length - 1})` : ''}`
      throw createError({ statusCode: 422, statusMessage: 'bad_rules', message, data: { message, errors } })
    }
    await agh('filtering/set_rules', { method: 'POST', body: { rules: lines } })
    return { text: userRules(await confirmFiltering(st => userRules(st) === lines.join('\n'), 'свои правила не сохранились')) }
  }),

  'GET /dns/filters': admin(async () => listsView(await filteringStatus())),
  /** Добавить список: AdGuard сам скачивает его и отвечает отказом, если не смог. */
  'POST /dns/filters': admin(async (event) => {
    const { name, url, white } = await readBodyAs(event, NewList)
    await agh('filtering/add_url', { method: 'POST', body: { name, url, whitelist: white }, timeout: 60_000 })
    return listsView(await confirmFiltering(st => !!findList(st, url, white), `список «${name}» не появился`))
  }),
  /** Включить или выключить список. */
  'PATCH /dns/filters': admin(async (event) => {
    const { url, white, enabled } = await readBodyAs(event, List.extend({ enabled: z.boolean() }))
    const f = findList(await filteringStatus(), url, white)
    if (!f) throw createError({ statusCode: 404, statusMessage: 'list_not_found', message: 'Такого списка уже нет в AdGuard' })
    await agh('filtering/set_url', { method: 'POST', body: { url, whitelist: white, data: { name: f.name, url, enabled } }, timeout: 60_000 })
    return listsView(await confirmFiltering(st => findList(st, url, white)?.enabled === enabled, `список «${f.name}» ${enabled ? 'не включился' : 'не выключился'}`))
  }),
  'DELETE /dns/filters': admin(async (event) => {
    const { url, white } = await readBodyAs(event, List)
    await agh('filtering/remove_url', { method: 'POST', body: { url, whitelist: white } })
    return listsView(await confirmFiltering(st => !findList(st, url, white), 'список остался'))
  }),
  /** «Проверить обновления»: AdGuard перекачивает включённые списки, чёрные и белые. */
  'POST /dns/filters/refresh': admin(async () => {
    let updated = 0
    for (const whitelist of [false, true]) {
      updated += (await agh<{ updated: number }>('filtering/refresh', { method: 'POST', body: { whitelist }, timeout: 120_000 })).updated ?? 0
    }
    return { updated, ...listsView(await filteringStatus()) }
  }),

  /** Перезаписи DNS из AdGuard. */
  'GET /dns/rewrites': admin(() => rewriteList()),
  /** Добавить перезапись. Дубль AdGuard принимает молча — отказываем сами. */
  'POST /dns/rewrites': admin(async (event) => {
    const b = await readBodyAs(event, z.object({ domain: z.string().max(253), answer: z.string().max(253) }))
    const rw = { domain: b.domain.trim().toLowerCase().replace(/\.$/, ''), answer: b.answer.trim() }
    const bad = checkRewrite(rw.domain, rw.answer)
    if (bad) throw createError({ statusCode: 422, statusMessage: 'bad_rewrite', message: bad })
    if ((await rewriteList()).some(x => sameRewrite(x, rw))) {
      throw createError({ statusCode: 409, statusMessage: 'rewrite_exists', message: `Перезапись «${rw.domain}» → ${rw.answer} уже есть` })
    }
    await agh('rewrite/add', { method: 'POST', body: { ...rw, enabled: true } })
    return confirmRewrites(l => l.some(x => sameRewrite(x, rw)), `перезапись «${rw.domain}» не появилась`)
  }),
  /** Включить или выключить перезапись. */
  'PATCH /dns/rewrites': admin(async (event) => {
    const { domain, answer, enabled } = await readBodyAs(event, Rewrite.extend({ enabled: z.boolean() }))
    const cur = await findRewrite(domain, answer)
    await agh('rewrite/update', { method: 'PUT', body: { target: cur, update: { domain, answer, enabled } } })
    return confirmRewrites(l => l.some(x => sameRewrite(x, cur) && x.enabled === enabled), `перезапись «${domain}» ${enabled ? 'не включилась' : 'не выключилась'}`)
  }),
  'DELETE /dns/rewrites': admin(async (event) => {
    const { domain, answer } = await readBodyAs(event, Rewrite)
    const cur = await findRewrite(domain, answer)
    await agh('rewrite/delete', { method: 'POST', body: { domain: cur.domain, answer: cur.answer } })
    return confirmRewrites(l => !l.some(x => sameRewrite(x, cur)), `перезапись «${domain}» не удалилась`)
  }),

  /** Каталог сервисов AdGuard и заблокированные для всех клиентов. */
  'GET /dns/services': admin(async () => {
    const [all, blocked] = await Promise.all([serviceCatalog(), blockedServices()])
    return { all: all.map(s => ({ id: s.id, name: s.name })), blocked: blocked.ids ?? [] }
  }),
  /** Заблокированные для всех сервисы — список целиком; расписание AdGuard не трогаем. */
  'PUT /dns/services': admin(async (event) => {
    const ids = [...new Set((await readBodyAs(event, z.object({ ids: z.array(z.string().max(64)).max(1000) }))).ids)]
    const known = new Set((await serviceCatalog()).map(s => s.id))
    const unknown = ids.find(id => !known.has(id))
    if (unknown) throw createError({ statusCode: 422, statusMessage: 'unknown_service', message: `Сервиса «${unknown}» нет в AdGuard` })
    const { schedule } = await blockedServices()
    await agh('blocked_services/update', { method: 'PUT', body: { ids, schedule } })
    const now = (await blockedServices()).ids ?? []
    if (now.length !== ids.length || ids.some(id => !now.includes(id))) {
      throw createError({ statusCode: 502, statusMessage: 'agh_not_applied', message: 'AdGuard не применил изменение: блокировка сервисов' })
    }
    return { blocked: now }
  }),

  /** Настройки DNS из AdGuard: серверы выше, защита, кэш, журнал. */
  'GET /dns/settings': admin(async () => viewOf(await settingsState())),
  /** Изменить одну настройку DNS и вернуть, что AdGuard показывает после. */
  'PATCH /dns/settings': admin(async (event) => {
    const b = await readBodyAs(event, Setting) as Partial<SettingsView> & { logHours?: number, statsHours?: number }
    const cur = await settingsState()
    const dns = (body: Record<string, unknown>) => agh('dns_config', { method: 'POST', body })
    const toggle = (path: string, on: boolean) => agh(`${path}/${on ? 'enable' : 'disable'}`, { method: 'POST' })
    if (b.upstreams !== undefined) {
      const list = upstreamLines(b.upstreams)
      if (!list.length) throw badSetting('Нужен хотя бы один сервер выше')
      try {
        await dns({ upstream_dns: list })
      }
      catch (e) {
        const x = e as { statusMessage?: string, message?: string }
        if (x.statusMessage !== 'agh_rejected') throw e
        throw badSetting(upstreamError(x.message ?? ''))
      }
    }
    else if (b.mode) await dns({ upstream_mode: b.mode })
    else if (b.bootstrap !== undefined) {
      const r = parseBootstrap(b.bootstrap)
      if ('error' in r) throw badSetting(r.error)
      await dns({ bootstrap_dns: r.list })
    }
    else if (b.safebrowsing !== undefined) await toggle('safebrowsing', b.safebrowsing)
    else if (b.parental !== undefined) await toggle('parental', b.parental)
    else if (b.safesearch !== undefined) await agh('safesearch/settings', { method: 'PUT', body: { ...cur.ss, enabled: b.safesearch } })
    else if (b.dnssec !== undefined) await dns({ dnssec_enabled: b.dnssec })
    else if (b.noAaaa !== undefined) await dns({ disable_ipv6: b.noAaaa })
    else if (b.optimistic !== undefined) await dns({ cache_optimistic: b.optimistic })
    else if (b.ratelimit !== undefined) {
      const n = parseRatelimit(b.ratelimit)
      if (n === null) throw badSetting('Лимит запросов: число в секунду или «без лимита»')
      await dns({ ratelimit: n })
    }
    else if (b.block !== undefined) {
      const r = parseBlock(b.block, cur.info)
      if ('error' in r) throw badSetting(r.error)
      if (r.set) await dns(r.set as Record<string, unknown>)
    }
    else if (b.logHours !== undefined) {
      if (!LOG_HOURS.includes(b.logHours)) throw badSetting('Журнал хранится 1, 7 или 90 дней')
      await agh('querylog/config/update', { method: 'PUT', body: { ...cur.log, interval: b.logHours * HOUR } })
    }
    else if (b.statsHours !== undefined) {
      if (!STATS_HOURS.includes(b.statsHours)) throw badSetting('Статистика хранится 24 часа, 7 или 30 дней')
      await agh('stats/config/update', { method: 'PUT', body: { ...cur.stats, interval: b.statsHours * HOUR } })
    }
    else if (b.anonymize !== undefined) await agh('querylog/config/update', { method: 'PUT', body: { ...cur.log, anonymize_client_ip: b.anonymize } })
    const view = viewOf(await settingsState())
    const expect: Record<string, unknown> = { ...b }
    if (b.upstreams !== undefined) expect.upstreams = upstreamLines(b.upstreams).join('\n')
    if (b.bootstrap !== undefined) expect.bootstrap = (parseBootstrap(b.bootstrap) as { list: string[] }).list.join(', ')
    if (b.ratelimit !== undefined) expect.ratelimit = undefined
    if (b.block !== undefined) expect.block = undefined
    const miss = Object.entries(expect).find(([k, v]) => v !== undefined && view[k as keyof SettingsView] !== v)
    if (miss) throw createError({ statusCode: 502, statusMessage: 'agh_not_applied', message: 'AdGuard не применил изменение настройки' })
    return view
  }),
  /** Очистить кэш DNS. */
  'POST /dns/settings/cache-clear': admin(async () => {
    await agh('cache_clear', { method: 'POST' })
    return { ok: true }
  }),
  /** Сбросить журнал запросов или статистику. */
  'POST /dns/settings/reset': admin(async (event) => {
    const { what } = await readBodyAs(event, z.object({ what: z.enum(['log', 'stats']) }))
    await agh(what === 'log' ? 'querylog_clear' : 'stats_reset', { method: 'POST' })
    return { ok: true }
  }),
  /** Проверить серверы выше: AdGuard спрашивает каждый и отвечает OK или текст ошибки. */
  'POST /dns/settings/upstreams-check': admin(async (event) => {
    const b = await readBodyAs(event, z.object({ upstreams: z.string().max(10_000), bootstrap: z.string().max(2000) }))
    const list = upstreamLines(b.upstreams)
    if (!list.length) throw badSetting('Нужен хотя бы один сервер выше')
    const boot = parseBootstrap(b.bootstrap)
    const res = await agh<Record<string, string>>('test_upstream_dns', {
      method: 'POST',
      body: { upstream_dns: list, bootstrap_dns: 'list' in boot ? boot.list : [], fallback_dns: [], private_upstream: [] },
      timeout: 20_000,
    })
    const failed = Object.entries(res).filter(([, v]) => v !== 'OK').map(([k]) => k)
    return { total: Object.keys(res).length, failed }
  }),
}
