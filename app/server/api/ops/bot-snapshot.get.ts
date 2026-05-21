import { desc } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { fetchConnections, fetchProxies } from '../../utils/clash-client'
import { fetchNodeMetrics, nodeInstances } from '../../utils/vm-client'

export default defineEventHandler(async (event) => {
  const cfg = useRuntimeConfig()
  const expected = String(cfg.tgbotSecret ?? '')
  if (!expected) {
    throw createError({ statusCode: 503, statusMessage: 'tgbot_secret_not_configured' })
  }

  const auth = getHeader(event, 'authorization') ?? ''
  const provided = auth.replace(/^Bearer\s+/i, '')
  if (provided !== expected) {
    throw createError({ statusCode: 401, statusMessage: 'unauthorized' })
  }

  const db = useDb()
  const [allClients, nodes, proxies, connections] = await Promise.all([
    db.select({
      id: clients.id,
      name: clients.name,
      enabled: clients.enabled,
      expiresAt: clients.expiresAt,
      createdAt: clients.createdAt,
    }).from(clients).orderBy(desc(clients.createdAt)),
    fetchNodeMetrics(nodeInstances().map(n => n.instance)),
    fetchProxies(),
    fetchConnections(),
  ])

  const instances = nodeInstances()
  const nodesPayload = instances.map((n, i) => ({
    tag: n.tag,
    label: n.label,
    cpu: nodes[i]?.cpuPct ?? null,
    ram: nodes[i]?.ramPct ?? null,
    rxMbps: nodes[i]?.rxBps != null ? nodes[i]!.rxBps! * 8 / 1e6 : null,
    txMbps: nodes[i]?.txBps != null ? nodes[i]!.txBps! * 8 / 1e6 : null,
    uptimeSec: nodes[i]?.uptimeSec ?? null,
    // возраст последних метрик ноды, сек — бот алертит при staleSec > 5 мин
    staleSec: nodes[i]?.staleSec ?? null,
  }))

  const outboundsPayload = proxies
    .filter(p => p.name.startsWith('hy2-') || p.name === 'direct-ru')
    .map((p) => {
      const latest = p.history?.length ? p.history[p.history.length - 1]!.delay : null
      const rtt = p.delay ?? latest
      return { name: p.name, rttMs: rtt, isWarp: p.name.endsWith('-warp') }
    })

  return {
    timestamp: new Date().toISOString(),
    clients: allClients.map(c => ({
      id: c.id,
      name: c.name,
      enabled: c.enabled,
      expiresAt: c.expiresAt,
      createdAt: c.createdAt,
    })),
    nodes: nodesPayload,
    outbounds: outboundsPayload,
    connections: connections?.connections.length ?? 0,
  }
})
