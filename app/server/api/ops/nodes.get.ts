import { requireAuth } from '../../utils/auth'
import { fetchConnections, fetchProxies } from '../../utils/clash-client'
import { exitNodeOf, readDisabledExits } from '../../utils/exit-control'
import { fetchDailyTraffic, fetchNodeMetrics, nodeInstances } from '../../utils/vm-client'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const nodes = nodeInstances()
  const hosts = new Map(
    String(useRuntimeConfig().nodeHosts || '').split(',')
      .map(kv => kv.split('=').map(s => s.trim()))
      .filter((kv): kv is [string, string] => kv.length === 2 && !!kv[0] && !!kv[1]),
  )
  const instances = nodes.map(n => n.instance)
  const [metrics, daily, disabled, proxies, conns] = await Promise.all([
    fetchNodeMetrics(instances),
    fetchDailyTraffic(instances),
    readDisabledExits(),
    fetchProxies(),
    fetchConnections(),
  ])
  // Узел, через который сейчас уходит иностранный трафик вне дорожек (foreign-best).
  const activeNode = exitNodeOf(proxies.find(p => p.name === 'foreign-best')?.now)
  // Дорожки lane-NN: сколько смотрит на каждый узел и сколько из них — через
  // его warp. При BALANCE=off дорожки повторяют foreign-best и все стоят на
  // одном узле — так и показываем. Дорожек нет (старый конфиг, clash API
  // молчит) — null, а не ноль.
  const lanes = proxies.filter(p => p.name.startsWith('lane-'))
  const lanesByNode = new Map<string, { total: number, warp: number }>()
  for (const l of lanes) {
    const node = exitNodeOf(l.now)
    if (!node) continue
    const s = lanesByNode.get(node) ?? { total: 0, warp: 0 }
    s.total++
    if (l.now!.endsWith('-warp')) s.warp++
    lanesByNode.set(node, s)
  }
  // Активные устройства на экзите: разные клиентские VPN-IP среди текущих
  // соединений через узел (mgmt-туннели — служебные, не считаем).
  const devicesByNode = new Map<string, Set<string>>()
  for (const c of conns?.connections ?? []) {
    const out = c.chains[0]
    const node = out && !out.endsWith('-mgmt') ? exitNodeOf(out) : null
    const ip = c.metadata?.sourceIP
    if (!node || !ip || !/^10\.6[678]\./.test(ip)) continue
    let s = devicesByNode.get(node)
    if (!s) { s = new Set(); devicesByNode.set(node, s) }
    s.add(ip)
  }
  return nodes.map((n, i) => ({
    tag: n.tag,
    label: n.label,
    host: hosts.get(n.tag) ?? null,
    instance: n.instance,
    cpu: metrics[i]?.cpuPct ?? null,
    iowait: metrics[i]?.iowaitPct ?? null,
    steal: metrics[i]?.stealPct ?? null,
    ram: metrics[i]?.ramPct ?? null,
    rxMbps: metrics[i]?.rxBps != null ? metrics[i]!.rxBps! * 8 / 1e6 : null,
    txMbps: metrics[i]?.txBps != null ? metrics[i]!.txBps! * 8 / 1e6 : null,
    uptimeSec: metrics[i]?.uptimeSec ?? null,
    diskFreeBytes: metrics[i]?.diskFreeBytes ?? null,
    diskFreePct: metrics[i]?.diskFreeBytes != null && metrics[i]?.diskSizeBytes
      ? 100 * metrics[i]!.diskFreeBytes! / metrics[i]!.diskSizeBytes!
      : null,
    staleSec: metrics[i]?.staleSec ?? null,
    rxTodayBytes: daily.get(n.instance)?.rxBytes ?? null,
    txTodayBytes: daily.get(n.instance)?.txBytes ?? null,
    disabled: n.tag !== 'ru' && disabled.includes(n.tag),
    active: n.tag !== 'ru' && n.tag === activeNode,
    activeDevices: n.tag !== 'ru' ? (devicesByNode.get(n.tag)?.size ?? 0) : null,
    lanes: n.tag !== 'ru' && lanes.length ? (lanesByNode.get(n.tag)?.total ?? 0) : null,
    lanesWarp: n.tag !== 'ru' && lanes.length ? (lanesByNode.get(n.tag)?.warp ?? 0) : null,
  }))
})
