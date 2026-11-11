import { eq, gte, sql } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../database/client'
import { clients, deviceTrafficHourly, devices } from '../database/schema'
import { activityDays } from '../utils/activity-days'
import { fetchAdguardStats } from '../utils/agh-client'
import { admin, bot } from '../utils/api'
import { fetchConnections, fetchProxies, userVisibleOutbounds } from '../utils/clash-client'
import { ONLINE_MIN_BYTES, recentBytesByClient } from '../utils/client-activity'
import { clientStatus } from '../utils/client-status'
import { actorOf, joinDetail, recentEvents, recordEvent } from '../utils/events'
import { exitNodeOf, readDisabledExits, toggleExit } from '../utils/exit-control'
import { exitTraffic } from '../utils/exit-traffic'
import { clientsNewestFirst } from '../utils/list-order'
import { isPanelReady } from '../utils/panel-ready'
import { PROMQL, fetchDailyTraffic, fetchNodeMetrics, nodeInstances } from '../utils/vm-client'
import { rangeQuery } from '../utils/vm-range'
import { wgEndpointHasAAAA } from '../utils/wg-endpoint-dns'

const HOURS = 24
// Спарклайн узла на обзоре: 13 точек с шагом опроса панели (2 с), дальше
// страница сама дописывает свежие значения из /api/ops/nodes.
const POINTS = 13
const STEP = 2
const EventsQuery = z.object({ limit: z.coerce.number().int().min(1).max(100).default(4) })

const mbps = (bps: number | null | undefined) => bps != null ? bps * 8 / 1e6 : null

export default {
  'GET /version': () => {
    // Стадия 30 ждёт этот ответ перед печатью журнала переноса предпочтений.
    // HTTP начинает слушать раньше завершения async init-плагина Nitro.
    if (!isPanelReady()) {
      throw createError({ statusCode: 503, statusMessage: 'panel_starting', message: 'Панель запускается' })
    }
    return { name: 'anysda-vpn2', version: '1.1.5', license: 'MIT' }
  },
  'GET /events': admin(async event => recentEvents((await getValidatedQuery(event, EventsQuery.parse)).limit)),
  'GET /wg-endpoint-warning': admin(async (event) => {
    setHeader(event, 'cache-control', 'no-store')
    const cfg = useRuntimeConfig()
    return { hasAAAA: !!cfg.wgEnabled && await wgEndpointHasAAAA(String(cfg.wgPublicHost || '')) }
  }),

  /** Сетка «Активность» на обзоре: клиенты в сети и трафик по суткам МСК. */
  'GET /ops/activity': admin(() => activityDays()),
  'GET /ops/adguard': admin(async () => {
    const snap = await fetchAdguardStats()
    return snap ? { available: true, ...snap } : { available: false }
  }),
  /** Трафик за сутки или неделю по экзитам для обзора: ?period=day|week. */
  'GET /ops/traffic': admin(event => exitTraffic(getQuery(event).period === 'week' ? 'week' : 'day')),

  /**
   * Клиенты онлайн и офлайн для обзора.
   *
   * total  — все клиенты;
   * online — сейчас в сети (трафик за 5 минут от ONLINE_MIN_BYTES, как в списке клиентов);
   * hourly — 24 последних часа, старый первым: сколько клиентов набрали за час
   *          от ONLINE_MIN_BYTES. Час без записей — 0, ряд не рвётся.
   */
  'GET /ops/clients-online': admin(async () => {
    const hourNow = Math.floor(Date.now() / 3_600_000) * 3600
    const since = hourNow - (HOURS - 1) * 3600
    const db = useDb()
    const [[count], recent, rows] = await Promise.all([
      db.select({ n: sql<number>`count(*)` }).from(clients),
      recentBytesByClient(),
      db
        .select({
          hour: deviceTrafficHourly.hour,
          clientId: devices.clientId,
          bytes: sql<number>`sum(${deviceTrafficHourly.rx} + ${deviceTrafficHourly.tx})`,
        })
        .from(deviceTrafficHourly)
        .innerJoin(devices, eq(devices.id, deviceTrafficHourly.deviceId))
        .where(gte(deviceTrafficHourly.hour, since))
        .groupBy(deviceTrafficHourly.hour, devices.clientId),
    ])
    const hourly = Array.from({ length: HOURS }, () => 0)
    for (const r of rows) {
      const i = Math.floor((Number(r.hour) - since) / 3600)
      if (i >= 0 && i < HOURS && Number(r.bytes) >= ONLINE_MIN_BYTES) hourly[i]!++
    }
    const online = [...recent.values()].filter(b => b >= ONLINE_MIN_BYTES).length
    return { total: Number(count?.n ?? 0), online, hourly }
  }),

  /** Недавняя загрузка CPU по узлам: { tag: [старое … новое] }, пропуск — null. */
  'GET /ops/nodes-cpu': admin(async () => {
    const end = Math.floor(Date.now() / 1000 / STEP) * STEP
    const series = await rangeQuery(PROMQL.cpu, end - (POINTS - 1) * STEP, end, STEP)
    return Object.fromEntries(nodeInstances().map(n => [n.tag, series.get(n.instance) ?? []]))
  }),

  'GET /ops/nodes': admin(async () => {
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
      const node = exitNodeOf(c.chains[0])
      const ip = c.metadata?.sourceIP
      if (!node || !ip || !/^10\.6[678]\./.test(ip)) continue
      let s = devicesByNode.get(node)
      if (!s) { s = new Set(); devicesByNode.set(node, s) }
      s.add(ip)
    }
    return nodes.map((n, i) => {
      const m = metrics[i]
      const exit = n.tag !== 'ru'
      return {
        tag: n.tag,
        label: n.label,
        host: hosts.get(n.tag) ?? null,
        instance: n.instance,
        cpu: m?.cpuPct ?? null,
        iowait: m?.iowaitPct ?? null,
        steal: m?.stealPct ?? null,
        ram: m?.ramPct ?? null,
        rxMbps: mbps(m?.rxBps),
        txMbps: mbps(m?.txBps),
        uptimeSec: m?.uptimeSec ?? null,
        diskFreeBytes: m?.diskFreeBytes ?? null,
        diskFreePct: m?.diskFreeBytes != null && m.diskSizeBytes ? 100 * m.diskFreeBytes / m.diskSizeBytes : null,
        staleSec: m?.staleSec ?? null,
        rxTodayBytes: daily.get(n.instance)?.rxBytes ?? null,
        txTodayBytes: daily.get(n.instance)?.txBytes ?? null,
        disabled: exit && disabled.includes(n.tag),
        active: exit && n.tag === activeNode,
        activeDevices: exit ? (devicesByNode.get(n.tag)?.size ?? 0) : null,
        lanes: exit && lanes.length ? (lanesByNode.get(n.tag)?.total ?? 0) : null,
        lanesWarp: exit && lanes.length ? (lanesByNode.get(n.tag)?.warp ?? 0) : null,
      }
    })
  }),

  'PATCH /ops/exits/:tag': admin(async (event, user) => {
    const tag = String(getRouterParam(event, 'tag') || '')
    const exits = nodeInstances().map(n => n.tag).filter(t => t !== 'ru')
    if (!exits.includes(tag)) throw createError({ statusCode: 404, statusMessage: 'unknown_exit' })
    const { disabled } = await readValidatedBody(event, z.object({ disabled: z.boolean() }).parse)
    // Хотя бы один экзит должен остаться включённым — иначе watchdog'у некуда
    // уводить иностранный трафик.
    // Прежнее состояние и запись журнала - внутри очереди переключений: иначе
    // одновременный PATCH успевает записать своё между чтением и записью.
    const written = await toggleExit(tag, disabled, exits, async (wasDisabled) => {
      useLogger().info({ tag, wasDisabled, disabled }, 'exit toggled manually')
      if (wasDisabled === disabled) return
      await recordEvent({
        type: disabled ? 'exit_disabled' : 'exit_enabled', level: 'info', object: `node:${tag}`, actor: actorOf(user),
        title: `${disabled ? 'Экзит выключен' : 'Экзит включён'} · ${tag.toUpperCase()}`, detail: joinDetail('вручную', actorOf(user)),
      })
    })
    if (!written) throw createError({ statusCode: 409, statusMessage: 'last_enabled_exit' })
    return { tag, disabled }
  }),

  // Снимок для бота: по нему бот шлёт тревоги по нодам и сводки.
  'GET /ops/bot-snapshot': bot(async () => {
    const instances = nodeInstances()
    const [allClients, nodes, proxies, connections] = await Promise.all([
      useDb().select({
        id: clients.id,
        name: clients.name,
        frozenManual: clients.frozenManual,
        expiresAt: clients.expiresAt,
        createdAt: clients.createdAt,
      }).from(clients).orderBy(...clientsNewestFirst),
      fetchNodeMetrics(instances.map(n => n.instance), false),
      fetchProxies(),
      fetchConnections(),
    ])
    return {
      timestamp: new Date().toISOString(),
      clients: allClients.map(c => ({
        id: c.id,
        name: c.name,
        status: clientStatus(c),
        expiresAt: c.expiresAt,
        createdAt: c.createdAt,
      })),
      nodes: instances.map((n, i) => ({
        tag: n.tag,
        label: n.label,
        cpu: nodes[i]?.cpuPct ?? null,
        ram: nodes[i]?.ramPct ?? null,
        rxMbps: mbps(nodes[i]?.rxBps),
        txMbps: mbps(nodes[i]?.txBps),
        uptimeSec: nodes[i]?.uptimeSec ?? null,
        // возраст последних метрик ноды, сек — бот алертит при staleSec > 5 мин
        staleSec: nodes[i]?.staleSec ?? null,
        // размер ноды — по нему бот выбирает пороги CPU/RAM и окно тревоги
        cores: nodes[i]?.cores ?? null,
        memTotalBytes: nodes[i]?.memTotalBytes ?? null,
      })),
      outbounds: userVisibleOutbounds(proxies).map(p => ({ name: p.name, rttMs: p.delay ?? p.history?.at(-1)?.delay ?? null, isWarp: p.name.endsWith('-warp') })),
      connections: connections?.connections.length ?? 0,
    }
  }),
}
