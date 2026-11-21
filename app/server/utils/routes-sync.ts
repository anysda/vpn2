import { createHash } from 'node:crypto'
import { isIPv4 } from 'node:net'
import { asc, eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { collectorState, routes } from '../database/schema'

// По очереди: иначе запись по устаревшему снимку базы могла лечь последней.
const routesQueue = serialQueue()
// Правила, которые роутер последними принял; без записи - пустой список, его кладёт установка.
const APPLIED = 'routes-applied'
const digest = (rows: unknown) => createHash('sha256').update(JSON.stringify(rows)).digest('hex')

const sync = (onlyStale: boolean) => routesQueue(async () => {
  const db = useDb()
  const rows = await db.select({ type: routes.type, value: routes.value, outbound: routes.outbound }).from(routes).orderBy(asc(routes.id))
  const value = digest(rows)
  const [applied] = await db.select().from(collectorState).where(eq(collectorState.name, APPLIED))
  if (onlyStale && (applied?.value ?? digest([])) === value) return
  await controlApply('settings.apply', { resource: 'routes', config: rows })
  await db.insert(collectorState).values({ name: APPLIED, value }).onConflictDoUpdate({ target: collectorState.name, set: { value } })
})

/** Правила панели - в manual-routes.json роутера. */
export const syncRoutesFile = () => sync(false)
/** Повтор cron: только если роутер ещё не принял то, что в базе. */
export const healRoutesFile = () => sync(true)

const DOMAIN_RE = /^\*?\.?[a-z0-9-]+(?:\.[a-z0-9-]+)+$/i
const CIDR_RE = /^(?:\d{1,3}\.){3}\d{1,3}(?:\/(?:\d|[12]\d|3[0-2]))?$/

export function detectRouteType(value: string): 'domain' | 'ip_cidr' | null {
  // Числовой адрес не должен становиться доменом, если октеты или маска
  // неверны: применитель маршрутов отвергнет его после успешного ответа API.
  if (/^[\d.]+(?:\/.*)?$/.test(value)) return CIDR_RE.test(value) && isIPv4(value.split('/')[0]!) ? 'ip_cidr' : null
  return DOMAIN_RE.test(value) ? 'domain' : null
}
