import { count, eq, gt, isNotNull, sql } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients, oneTimeLinks, routes } from '../database/schema'

const startedAt = Date.now()

export default defineEventHandler(async (event) => {
  const db = useDb()
  const now = new Date()

  const [
    [enabledRow],
    [disabledRow],
    [activeOtlRow],
    [routesRow],
  ] = await Promise.all([
    db.select({ n: count() }).from(clients).where(eq(clients.enabled, true)),
    db.select({ n: count() }).from(clients).where(eq(clients.enabled, false)),
    db.select({ n: count() }).from(oneTimeLinks).where(gt(oneTimeLinks.expiresAt, now)),
    db.select({ n: count() }).from(routes),
  ])

  const [expiringRow] = await db
    .select({ n: count() })
    .from(clients)
    .where(sql`${clients.enabled} = 1 AND ${clients.expiresAt} IS NOT NULL AND ${clients.expiresAt} < ${
      new Date(now.getTime() + 7 * 86_400_000)
    }`)

  const uptimeSec = (Date.now() - startedAt) / 1000

  const lines: string[] = [
    '# HELP anysda_vpn2_clients_total Number of SS clients by enabled status',
    '# TYPE anysda_vpn2_clients_total gauge',
    `anysda_vpn2_clients_total{enabled="true"} ${enabledRow?.n ?? 0}`,
    `anysda_vpn2_clients_total{enabled="false"} ${disabledRow?.n ?? 0}`,
    '',
    '# HELP anysda_vpn2_clients_expiring_7d Clients expiring within 7 days',
    '# TYPE anysda_vpn2_clients_expiring_7d gauge',
    `anysda_vpn2_clients_expiring_7d ${expiringRow?.n ?? 0}`,
    '',
    '# HELP anysda_vpn2_one_time_links_active One-time links not yet expired',
    '# TYPE anysda_vpn2_one_time_links_active gauge',
    `anysda_vpn2_one_time_links_active ${activeOtlRow?.n ?? 0}`,
    '',
    '# HELP anysda_vpn2_routes_total Manual routing rules in DB',
    '# TYPE anysda_vpn2_routes_total gauge',
    `anysda_vpn2_routes_total ${routesRow?.n ?? 0}`,
    '',
    '# HELP anysda_vpn2_panel_uptime_seconds Panel process uptime',
    '# TYPE anysda_vpn2_panel_uptime_seconds counter',
    `anysda_vpn2_panel_uptime_seconds ${uptimeSec.toFixed(0)}`,
    '',
    '# HELP anysda_vpn2_info Panel build info',
    '# TYPE anysda_vpn2_info gauge',
    'anysda_vpn2_info{version="0.1.0",license="MIT"} 1',
    '',
  ]

  setHeader(event, 'content-type', 'text/plain; version=0.0.4; charset=utf-8')
  return lines.join('\n')
})
