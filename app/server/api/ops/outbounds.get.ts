import { requireAuth } from '../../utils/auth'
import { fetchConnections, fetchProxies } from '../../utils/clash-client'

// Track previous totals per outbound to compute deltas across calls
const lastTotals = new Map<string, { up: number, down: number, ts: number }>()

export default defineEventHandler(async (event) => {
  await requireAuth(event)

  const [proxies, connections] = await Promise.all([
    fetchProxies(),
    fetchConnections(),
  ])

  // Aggregate live throughput per outbound from /connections
  const upPerChain = new Map<string, number>()
  const downPerChain = new Map<string, number>()
  if (connections) {
    for (const c of connections.connections) {
      const tag = c.chains[0]
      if (!tag) continue
      upPerChain.set(tag, (upPerChain.get(tag) ?? 0) + c.upload)
      downPerChain.set(tag, (downPerChain.get(tag) ?? 0) + c.download)
    }
  }

  const now = Date.now()

  return proxies
    .filter(p => p.name.startsWith('hy2-') || p.name === 'direct-ru')
    .map((p) => {
      const latestDelay = p.history?.length
        ? p.history[p.history.length - 1]!.delay
        : null
      const rtt = p.delay ?? latestDelay ?? null
      const status: 'up' | 'down' | 'warn'
        = rtt === null || rtt === 0
          ? 'down'
          : rtt > 300
            ? 'warn'
            : 'up'

      const upTotal = upPerChain.get(p.name) ?? 0
      const downTotal = downPerChain.get(p.name) ?? 0
      const prev = lastTotals.get(p.name)
      let upKbps = 0
      let downKbps = 0
      if (prev) {
        const dt = (now - prev.ts) / 1000
        if (dt > 0 && dt < 30) {
          upKbps = Math.max(0, (upTotal - prev.up) * 8 / 1000 / dt)
          downKbps = Math.max(0, (downTotal - prev.down) * 8 / 1000 / dt)
        }
      }
      lastTotals.set(p.name, { up: upTotal, down: downTotal, ts: now })

      return {
        name: p.name,
        rttMs: rtt,
        status,
        upKbps: Number(upKbps.toFixed(1)),
        downKbps: Number(downKbps.toFixed(1)),
        isWarp: p.name.endsWith('-warp'),
      }
    })
})
