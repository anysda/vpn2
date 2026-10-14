import { desc } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { clients, devices } from '../../database/schema'
import { agh } from '../../utils/agh-client'
import type { AghStats } from '../../utils/agh-parse'
import { requireAuth } from '../../utils/auth'
import { serviceNames } from '../../utils/dns-context'
import { clientRows, type AghPersistentClient, type PanelClient } from '../../utils/dns-settings'

/** Клиенты панели глазами AdGuard: адреса в туннеле, свои настройки, запросы. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const db = useDb()
  const [cl, dv, aghClients, stats, names] = await Promise.all([
    db.select().from(clients).orderBy(desc(clients.createdAt)),
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
})
