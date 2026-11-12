import { isIPv4 } from 'node:net'
import { asc } from 'drizzle-orm'
import { useDb } from '../database/client'
import { routes } from '../database/schema'

/** Правила панели - в manual-routes.json роутера; число записанных правил. */
export async function syncRoutesFile(): Promise<number> {
  const rows = await useDb().select({ type: routes.type, value: routes.value, outbound: routes.outbound }).from(routes).orderBy(asc(routes.id))
  await controlApply('settings.apply', { resource: 'routes', config: rows })
  useLogger().info({ count: rows.length }, 'routes synced')
  return rows.length
}

const DOMAIN_RE = /^\*?\.?[a-z0-9-]+(?:\.[a-z0-9-]+)+$/i
const CIDR_RE = /^(?:\d{1,3}\.){3}\d{1,3}(?:\/(?:\d|[12]\d|3[0-2]))?$/

export function detectRouteType(value: string): 'domain' | 'ip_cidr' | null {
  // Числовой адрес не должен становиться доменом, если октеты или маска
  // неверны: применитель маршрутов отвергнет его после успешного ответа API.
  if (/^[\d.]+(?:\/.*)?$/.test(value)) return CIDR_RE.test(value) && isIPv4(value.split('/')[0]!) ? 'ip_cidr' : null
  return DOMAIN_RE.test(value) ? 'domain' : null
}
