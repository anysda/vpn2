import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { asc } from 'drizzle-orm'
import { useDb } from '../database/client'
import { routes } from '../database/schema'

export async function syncRoutesFile(): Promise<{ count: number, path: string }> {
  const cfg = useRuntimeConfig()
  const log = useLogger()
  const db = useDb()

  const rows = await db
    .select({ type: routes.type, value: routes.value, outbound: routes.outbound })
    .from(routes)
    .orderBy(asc(routes.id))

  const path = cfg.routesFilePath as string
  try {
    const dir = dirname(path)
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
    writeFileSync(path, JSON.stringify(rows, null, 2) + '\n', { mode: 0o644 })
  }
  catch (err) {
    log.error({ err, path }, 'failed to write manual-routes.json')
    throw err
  }

  log.info({ count: rows.length, path }, 'routes synced')
  return { count: rows.length, path }
}

const DOMAIN_RE = /^\*?\.?[a-z0-9-]+(?:\.[a-z0-9-]+)+$/i
const CIDR_RE = /^(?:\d{1,3}\.){3}\d{1,3}(?:\/(?:\d|[12]\d|3[0-2]))?$/

export function detectRouteType(value: string): 'domain' | 'ip_cidr' | null {
  if (CIDR_RE.test(value)) return 'ip_cidr'
  if (DOMAIN_RE.test(value)) return 'domain'
  return null
}
