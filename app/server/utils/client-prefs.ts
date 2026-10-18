import { controlApply } from './control'
import { eq, isNotNull } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients, collectorState, devices } from '../database/schema'

const EXIT_MAP_STATE = 'client-exit-hosts'

function exitHosts(value: string): Record<string, string> {
  return Object.fromEntries(value.split(',').flatMap((pair) => {
    const at = pair.indexOf('=')
    const tag = pair.slice(0, at).trim()
    const host = pair.slice(at + 1).trim().toLowerCase().replace(/\.$/, '')
    return at > 0 && tag !== 'ru' && host ? [[tag, host]] : []
  }))
}

/** Карта и предпочтения меняются вместе; обновляем по id, чтобы обмен тегами не сливал клиентов. */
async function reconcileExits(): Promise<void> {
  const cfg = useRuntimeConfig()
  const tags = String(cfg.exitTags || '').trim().split(/\s+/).filter(Boolean)
  // Без списка экзитов (например, отдельный запуск панели) перенос невозможен.
  if (!tags.length) return
  const hosts = exitHosts(String(cfg.nodeHosts || ''))
  if (tags.some(tag => !Object.hasOwn(hosts, tag))) throw new Error('client exit map incomplete: missing exit address')
  const current = Object.fromEntries(tags.sort().map(tag => [tag, hosts[tag]!]))
  const value = JSON.stringify(current)
  const db = useDb()
  // Обычная синхронизация устройств не должна брать SQLite write-lock:
  // карта меняется только при деплое. В транзакции проверяем её повторно.
  const [known] = await db.select().from(collectorState).where(eq(collectorState.name, EXIT_MAP_STATE))
  if (known?.value === value) return
  const changes = await db.transaction(async (tx) => {
    const [saved] = await tx.select().from(collectorState).where(eq(collectorState.name, EXIT_MAP_STATE))
    if (saved?.value === value) return []
    // При первом обновлении со старой панели стадия 30 передаёт её NUXT_NODE_HOSTS.
    // В дальнейшем карта в БД переживает рестарты, восстановление и повтор деплоя.
    const previous: Record<string, string> = saved ? JSON.parse(saved.value) : exitHosts(String(cfg.previousNodeHosts || ''))
    const rows = await tx.select({ id: clients.id, oldTag: clients.preferredExit })
      .from(clients).where(isNotNull(clients.preferredExit))
    const changed = []
    for (const row of rows) {
      const oldTag = row.oldTag!
      const matches = Object.keys(current).filter(tag => current[tag] === previous[oldTag])
      // Адрес однозначно узнаёт переименование, включая перестановку тегов.
      // Если адрес сменился, существующий тег сохраняет выбор владельца.
      const newTag = matches.length === 1 ? matches[0]! : Object.hasOwn(current, oldTag) ? oldTag : null
      if (newTag === oldTag) continue
      await tx.update(clients).set({ preferredExit: newTag, updatedAt: new Date() }).where(eq(clients.id, row.id))
      changed.push({ clientId: row.id, oldTag, newTag })
    }
    await tx.insert(collectorState).values({ name: EXIT_MAP_STATE, value })
      .onConflictDoUpdate({ target: collectorState.name, set: { value } })
    return changed
  })
  for (const change of changes) {
    useLogger().info(change, change.newTag
      ? `client exit preference: ${change.oldTag} → ${change.newTag}`
      : `client exit preference: ${change.oldTag} → авто (экзит удалён или адрес неоднозначен)`)
  }
}

/**
 * Файл предпочитаемых экзитов для роутера: { "<узел>": ["10.66.66.2", …] }.
 * anysda-apply-routes (стадия 20) кладёт IP каждого узла в свой набор правил
 * client-pref-<узел> с выходом pin-hy2-<узел>-direct: sing-box перечитывает
 * набор на ходу, соединения не рвутся. Пока узел выключен, мёртв или в
 * штрафной, failover-watchdog держит pin на foreign-best. Берём постоянные IP
 * устройства во всех трёх протоколах: WireGuard, OpenVPN (ccd ifconfig-push)
 * и IKEv2 (пул из одного адреса на устройство).
 */
async function writePrefs(): Promise<void> {
  await reconcileExits()
  const rows = await useDb()
    .select({ node: clients.preferredExit, wg: devices.wgIp, ovpn: devices.ovpnIp, ikev2: devices.ikev2Ip })
    .from(devices)
    .innerJoin(clients, eq(clients.id, devices.clientId))
    .where(isNotNull(clients.preferredExit))

  const byNode: Record<string, Set<string>> = Object.create(null)
  for (const r of rows) {
    for (const ip of [r.wg, r.ovpn, r.ikev2]) {
      if (r.node && ip) (byNode[r.node] ??= new Set()).add(ip)
    }
  }
  await controlApply('settings.apply', {
    resource: 'preferences',
    config: Object.fromEntries(Object.keys(byNode).sort().map(n => [n, [...byNode[n]!].sort()])),
  })
}

// По очереди: иначе запись по устаревшему снимку базы могла лечь последней.
let queue: Promise<unknown> = Promise.resolve()

export function syncClientPrefsFile(): Promise<void> {
  const run = queue.then(writePrefs)
  queue = run.catch(() => {})
  return run
}
