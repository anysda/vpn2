import { eq, isNotNull } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients, collectorState, devices } from '../database/schema'

const EXIT_MAP_STATE = 'client-exit-hosts'

const plainHost = (host: string) => host.trim().toLowerCase().replace(/\.$/, '')

function exitHosts(value: string): Record<string, string> {
  return Object.fromEntries(value.split(',').flatMap((pair) => {
    const at = pair.indexOf('=')
    const tag = pair.slice(0, at).trim()
    const host = plainHost(pair.slice(at + 1))
    return at > 0 && tag !== 'ru' && host ? [[tag, host]] : []
  }))
}

/** Карта и предпочтения меняются вместе; обновляем по id, чтобы обмен тегами не сливал клиентов. */
async function reconcileExits(): Promise<void> {
  const cfg = useRuntimeConfig()
  const exits = exitNodes()
  // Без списка экзитов (например, отдельный запуск панели) перенос невозможен.
  if (!exits.length) return
  if (exits.some(n => !plainHost(n.host))) throw new Error('client exit map incomplete: missing exit address')
  const hosts = new Map(exits.map(n => [n.tag, plainHost(n.host)]))
  const current = Object.fromEntries([...hosts.keys()].sort().map(tag => [tag, hosts.get(tag)!]))
  const value = JSON.stringify(current)
  const db = useDb()
  // Обычная синхронизация не должна брать write-lock: карта меняется только при деплое.
  const [known] = await db.select().from(collectorState).where(eq(collectorState.name, EXIT_MAP_STATE))
  if (known?.value === value) return
  const changes = await db.transaction(async (tx) => {
    const [saved] = await tx.select().from(collectorState).where(eq(collectorState.name, EXIT_MAP_STATE))
    if (saved?.value === value) return []
    // Пока карты в базе нет, её засевает карта прежней панели (NUXT_PREVIOUS_NODE_HOSTS).
    const previous: Record<string, string> = saved ? JSON.parse(saved.value) : exitHosts(String(cfg.previousNodeHosts || ''))
    const rows = await tx.select({ id: clients.id, name: clients.name, oldTag: clients.preferredExit }).from(clients).where(isNotNull(clients.preferredExit))
    const changed = []
    for (const row of rows) {
      const oldTag = row.oldTag!
      const matches = Object.keys(current).filter(tag => current[tag] === previous[oldTag])
      // Адрес узнаёт переименование тега; если адрес сменился, существующий тег сохраняет выбор.
      const newTag = matches.length === 1 ? matches[0]! : Object.hasOwn(current, oldTag) ? oldTag : null
      if (newTag === oldTag) continue
      await tx.update(clients).set({ preferredExit: newTag, updatedAt: new Date() }).where(eq(clients.id, row.id))
      changed.push({ clientId: row.id, name: row.name, oldTag, newTag })
    }
    await tx.insert(collectorState).values({ name: EXIT_MAP_STATE, value })
      .onConflictDoUpdate({ target: collectorState.name, set: { value } })
    return changed
  })
  for (const { name, ...change } of changes) {
    useLogger().info(change, `client exit preference: ${change.oldTag} → ${change.newTag ?? 'авто (экзит удалён или адрес неоднозначен)'}`)
    if (change.newTag) continue
    const detail = `экзит ${change.oldTag.toUpperCase()} удалён`
    await recordEvent({ type: 'client_exit_unpinned', level: 'warn', object: `client:${change.clientId}`, title: `Закрепление снято · ${name}`, detail })
    await notifyWarning(notifyBot('system_alert', { subject: 'Закрепление снято', detail: `${name} · ${detail}` }, `Закрепление снято · ${name}`))
  }
}

/**
 * Предпочитаемые экзиты для роутера: { "<узел>": ["10.66.66.2", …] } с
 * постоянными адресами устройства во всех трёх протоколах. sing-box
 * перечитывает набор на ходу, соединения не рвутся.
 */
async function writePrefs(): Promise<void> {
  await reconcileExits()
  const rows = await useDb().select({ node: clients.preferredExit, wg: devices.wgIp, ovpn: devices.ovpnIp, ikev2: devices.ikev2Ip })
    .from(devices).innerJoin(clients, eq(clients.id, devices.clientId)).where(isNotNull(clients.preferredExit))

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
const prefsQueue = serialQueue()
export const syncClientPrefsFile = () => prefsQueue(writePrefs)
