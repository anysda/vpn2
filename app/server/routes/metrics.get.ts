import { count, sql } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients, devices, routes } from '../database/schema'

const startedAt = Date.now()

export default defineEventHandler(async (event) => {
  const db = useDb()
  const nowSec = Math.floor(Date.now() / 1000)
  const soonSec = nowSec + 7 * 86_400

  // Активен = не заморожен вручную И срок не истёк (см. utils/client-status.ts).
  const activeCond = sql`${clients.frozenManual} = 0 AND (${clients.expiresAt} IS NULL OR ${clients.expiresAt} > ${nowSec})`

  const [
    [totalRow],
    [activeRow],
    [devicesRow],
    [routesRow],
    [expiringRow],
  ] = await Promise.all([
    db.select({ n: count() }).from(clients),
    db.select({ n: count() }).from(clients).where(activeCond),
    db.select({ n: count() }).from(devices),
    db.select({ n: count() }).from(routes),
    db.select({ n: count() }).from(clients).where(
      sql`${clients.expiresAt} IS NOT NULL AND ${clients.expiresAt} > ${nowSec} AND ${clients.expiresAt} < ${soonSec}`,
    ),
  ])

  const total = totalRow?.n ?? 0
  const active = activeRow?.n ?? 0
  const frozen = total - active
  const uptimeSec = (Date.now() - startedAt) / 1000

  const lines: string[] = [
    '# HELP anysda_vpn2_clients_total Number of clients by status',
    '# TYPE anysda_vpn2_clients_total gauge',
    `anysda_vpn2_clients_total{status="active"} ${active}`,
    `anysda_vpn2_clients_total{status="frozen"} ${frozen}`,
    '',
    '# HELP anysda_vpn2_devices_total Number of devices across all clients',
    '# TYPE anysda_vpn2_devices_total gauge',
    `anysda_vpn2_devices_total ${devicesRow?.n ?? 0}`,
    '',
    '# HELP anysda_vpn2_clients_expiring_7d Clients expiring within 7 days',
    '# TYPE anysda_vpn2_clients_expiring_7d gauge',
    `anysda_vpn2_clients_expiring_7d ${expiringRow?.n ?? 0}`,
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
