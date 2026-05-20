import { requireAuth } from '../../utils/auth'
import { fetchNodeMetrics, nodeInstances } from '../../utils/vm-client'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const nodes = nodeInstances()
  const metrics = await fetchNodeMetrics(nodes.map(n => n.instance))
  return nodes.map((n, i) => ({
    tag: n.tag,
    label: n.label,
    instance: n.instance,
    cpu: metrics[i]?.cpuPct ?? null,
    ram: metrics[i]?.ramPct ?? null,
    rxMbps: metrics[i]?.rxBps != null ? metrics[i]!.rxBps! * 8 / 1e6 : null,
    txMbps: metrics[i]?.txBps != null ? metrics[i]!.txBps! * 8 / 1e6 : null,
    uptimeSec: metrics[i]?.uptimeSec ?? null,
    staleSec: metrics[i]?.staleSec ?? null,
  }))
})
