import { eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'
import { agh } from './agh-client'
import type { CheckHost } from './agh-parse'
import { sameRewrite, type Rewrite } from './dns-settings'

export interface AghFilter {
  id: number
  name: string
  url: string
  enabled: boolean
  rules_count: number
  last_updated?: string
}

export interface AghFilteringStatus {
  enabled: boolean
  interval: number
  filters: AghFilter[] | null
  whitelist_filters: AghFilter[] | null
  user_rules: string[] | null
}

interface AghService { id: string, name: string, icon_svg?: string, group_id?: string }

/** Адрес в туннеле → имя клиента панели (по всем протоколам устройства). */
export async function panelNames(): Promise<Map<string, string>> {
  const rows = await useDb()
    .select({ name: clients.name, wg: devices.wgIp, ovpn: devices.ovpnIp, ikev2: devices.ikev2Ip })
    .from(devices)
    .innerJoin(clients, eq(devices.clientId, clients.id))
  const names = new Map<string, string>()
  for (const r of rows) {
    for (const ip of [r.wg, r.ovpn, r.ikev2]) if (ip) names.set(ip, r.name)
  }
  return names
}

export async function filteringStatus(): Promise<AghFilteringStatus> {
  return agh<AghFilteringStatus>('filtering/status')
}

/** id списка → название, чёрные и белые вместе. */
export function listNames(st: AghFilteringStatus): Map<number, string> {
  return new Map([...(st.filters ?? []), ...(st.whitelist_filters ?? [])].map(f => [f.id, f.name]))
}

let services: AghService[] | null = null

/** Каталог сервисов AdGuard не меняется без обновления самого AdGuard. */
export async function serviceCatalog(): Promise<AghService[]> {
  services ??= (await agh<{ blocked_services: AghService[] }>('blocked_services/all')).blocked_services
  return services
}

export async function serviceNames(): Promise<Map<string, string>> {
  return new Map((await serviceCatalog()).map(s => [s.id, s.name]))
}

export interface ListRow { name: string, url: string, enabled: boolean, rules: number, updated: string | null }

/** Чёрные и белые списки для вкладки «Фильтры». */
export function listsView(st: AghFilteringStatus) {
  const row = (f: AghFilter): ListRow => ({ name: f.name, url: f.url, enabled: f.enabled, rules: f.rules_count, updated: f.last_updated ?? null })
  return { interval: st.interval, black: (st.filters ?? []).map(row), white: (st.whitelist_filters ?? []).map(row) }
}

/**
 * Состояние AdGuard после действия: если /control/filtering/status показывает
 * не то, что просили, человеку — ошибка, а не тост «готово».
 */
export async function confirmFiltering(ok: (st: AghFilteringStatus) => boolean, what: string): Promise<AghFilteringStatus> {
  const st = await filteringStatus()
  if (!ok(st)) throw createError({ statusCode: 502, statusMessage: 'agh_not_applied', message: `AdGuard не применил изменение: ${what}` })
  return st
}

export function findList(st: AghFilteringStatus, url: string, white: boolean): AghFilter | undefined {
  return ((white ? st.whitelist_filters : st.filters) ?? []).find(f => f.url === url)
}

/**
 * check_host, пока ответ не станет нужным, но не дольше `ms`. Правила и
 * списки AdGuard применяет не сразу: после set_rules / set_url движок
 * пересобирается в фоне (на 180 тыс. правил — около полсекунды), и до этого
 * check_host отвечает по-старому.
 */
export async function checkUntil(name: string, client: string | undefined, done: (r: CheckHost) => boolean, ms = 5000): Promise<CheckHost> {
  const until = Date.now() + ms
  for (;;) {
    const r = await agh<CheckHost>('filtering/check_host', { query: { name, client } })
    if (done(r) || Date.now() >= until) return r
    await new Promise(res => setTimeout(res, 150))
  }
}

/** Ответ ссылается на своё правило, которого уже нет, — движок ещё старый. */
export const citesGone = (r: CheckHost, gone: string[]) =>
  !!r.rules?.some(x => x.filter_list_id === 0 && gone.includes(x.text))

/** Перезаписи DNS: AdGuard отдаёт null, когда их нет. */
export async function rewriteList(): Promise<Rewrite[]> {
  return (await agh<Rewrite[] | null>('rewrite/list')) ?? []
}

/** Перезапись, как она лежит в AdGuard, или 404. */
export async function findRewrite(domain: string, answer: string): Promise<Rewrite> {
  const cur = (await rewriteList()).find(x => sameRewrite(x, { domain, answer }))
  if (!cur) throw createError({ statusCode: 404, statusMessage: 'rewrite_not_found', message: `Перезаписи «${domain}» → ${answer} нет` })
  return cur
}

/** Список перезаписей после действия; не то, что просили, — 502. */
export async function confirmRewrites(ok: (l: Rewrite[]) => boolean, what: string): Promise<Rewrite[]> {
  const l = await rewriteList()
  if (!ok(l)) throw createError({ statusCode: 502, statusMessage: 'agh_not_applied', message: `AdGuard не применил изменение: ${what}` })
  return l
}

export interface BlockedServices { ids: string[] | null, schedule?: Record<string, unknown> }

export async function blockedServices(): Promise<BlockedServices> {
  return agh<BlockedServices>('blocked_services/get')
}
