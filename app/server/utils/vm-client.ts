import { useLogger } from './logger'

interface VmInstantResult {
  metric: Record<string, string>
  value: [number, string]
}

interface VmResponse {
  status: 'success' | 'error'
  data: {
    resultType: 'vector' | 'matrix' | 'scalar' | 'string'
    result: VmInstantResult[]
  }
}

export interface NodeMetrics {
  instance: string
  cpuPct: number | null
  ramPct: number | null
  rxBps: number | null
  txBps: number | null
  uptimeSec: number | null
}

const PROMQL = {
  cpu: '100 * (1 - avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[30s])))',
  ram: '100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)',
  rx: 'sum by(instance) (irate(node_network_receive_bytes_total{device!="lo",device!~"wg.*"}[30s]))',
  tx: 'sum by(instance) (irate(node_network_transmit_bytes_total{device!="lo",device!~"wg.*"}[30s]))',
  uptime: 'node_time_seconds - node_boot_time_seconds',
}

// holdover[instance][metricKey] = last successful value
const holdover = new Map<string, Map<string, number>>()

function rememberValue(instance: string, metricKey: string, value: number) {
  let m = holdover.get(instance)
  if (!m) { m = new Map(); holdover.set(instance, m) }
  m.set(metricKey, value)
}

function recallValue(instance: string, metricKey: string): number | null {
  return holdover.get(instance)?.get(metricKey) ?? null
}

async function instantQuery(promql: string): Promise<Map<string, number>> {
  const url = useRuntimeConfig().vmUrl as string
  const result = new Map<string, number>()
  try {
    const data = await $fetch<VmResponse>(`${url}/api/v1/query`, {
      query: { query: promql },
      timeout: 3000,
    })
    if (data.status !== 'success') return result
    for (const row of data.data.result) {
      const instance = row.metric.instance
      if (!instance) continue
      const value = Number(row.value[1])
      if (!Number.isFinite(value)) continue
      result.set(instance, value)
    }
  }
  catch (err) {
    useLogger().warn({ err, promql }, 'VM query failed')
  }
  return result
}

function withHoldover(
  current: Map<string, number>,
  instances: string[],
  metricKey: string,
): Map<string, number | null> {
  const result = new Map<string, number | null>()
  for (const inst of instances) {
    const v = current.get(inst)
    if (v !== undefined) {
      rememberValue(inst, metricKey, v)
      result.set(inst, v)
    }
    else {
      result.set(inst, recallValue(inst, metricKey))
    }
  }
  return result
}

export async function fetchNodeMetrics(instances: string[]): Promise<NodeMetrics[]> {
  const [cpu, ram, rx, tx, uptime] = await Promise.all([
    instantQuery(PROMQL.cpu),
    instantQuery(PROMQL.ram),
    instantQuery(PROMQL.rx),
    instantQuery(PROMQL.tx),
    instantQuery(PROMQL.uptime),
  ])

  const cpuH = withHoldover(cpu, instances, 'cpu')
  const ramH = withHoldover(ram, instances, 'ram')
  const rxH = withHoldover(rx, instances, 'rx')
  const txH = withHoldover(tx, instances, 'tx')
  const upH = withHoldover(uptime, instances, 'uptime')

  return instances.map(inst => ({
    instance: inst,
    cpuPct: cpuH.get(inst) ?? null,
    ramPct: ramH.get(inst) ?? null,
    rxBps: rxH.get(inst) ?? null,
    txBps: txH.get(inst) ?? null,
    uptimeSec: upH.get(inst) ?? null,
  }))
}

/** Map MGMT_IPs to {tag, ip}. Tags from runtimeConfig.exitTags + 'ru' for .1 */
export function nodeInstances(): Array<{ tag: string, instance: string, label: string }> {
  const cfg = useRuntimeConfig()
  const prefix = cfg.mgmtMeshIpPrefix as string
  const tags = String(cfg.exitTags || '').trim().split(/\s+/).filter(Boolean)
  const nodes: Array<{ tag: string, instance: string, label: string }> = [
    { tag: 'ru', instance: `${prefix}1:9100`, label: 'RU entry' },
  ]
  tags.forEach((tag, i) => {
    nodes.push({
      tag,
      instance: `${prefix}${i + 2}:9100`,
      label: `${tag.toUpperCase()} exit`,
    })
  })
  return nodes
}
