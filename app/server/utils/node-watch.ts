import { fetchProxies } from './clash-client'
import { lastEventOf, recordEvent } from './events'
import { exitNodeOf } from './exit-control'
import { fetchNodeHealth, nodeInstances } from './vm-client'

/**
 * Сторож нод для журнала: раз в 30 с смотрит VictoriaMetrics и пишет только
 * переходы состояния. Telegram-алерты нод шлёт бот по своему опросу, панель
 * их не дублирует — здесь только запись в ленту.
 */

export const NODE_CHECK_MS = 30_000
const LAG_SEC = 60
// Тот же порог «оффлайн», что и у индикатора нод на Обзоре.
const OFFLINE_SEC = 180
const IOWAIT_PCT = 30
// Пропавшая серия (рестарт VM, сбой скрейпа) — оффлайн только со второй проверки подряд.
const MISSING_HITS = 2
const NODE_TYPES = ['node_offline', 'node_lag', 'node_online']

type NodeState = 'online' | 'lagging' | 'offline'

interface Track {
  state: NodeState
  since: number
  missing: number
  ioHits: number
  ioAlerted: boolean
}

const tracks = new Map<string, Track>()
let activeExit: string | null = null

const nodeObject = (tag: string) => `node:${tag}`
const nodeName = (tag: string) => tag.toUpperCase()
const CHECK_NOTE = 'проверка каждые 30 с'

async function trackOf(tag: string): Promise<Track> {
  const known = tracks.get(tag)
  if (known) return known
  // После рестарта панели состояние берём из журнала: без повторной записи
  // и с настоящим началом простоя.
  const last = await lastEventOf(nodeObject(tag), NODE_TYPES)
  const state: NodeState = last?.type === 'node_offline' ? 'offline' : last?.type === 'node_lag' ? 'lagging' : 'online'
  const track = { state, since: last?.at ?? 0, missing: 0, ioHits: 0, ioAlerted: false }
  tracks.set(tag, track)
  return track
}

function downtimeText(ms: number): string {
  return `простой ${Math.max(1, Math.round(ms / 60_000))} мин`
}

/** Один проход по нодам. VM недоступна — исключение, состояние не меняется. */
export async function checkNodes(now = Date.now()): Promise<void> {
  const nodes = nodeInstances()
  const health = await fetchNodeHealth(nodes.map(n => n.instance))
  for (const node of nodes) {
    const h = health.find(x => x.instance === node.instance)
    if (!h) continue
    const track = await trackOf(node.tag)
    const object = nodeObject(node.tag)
    const name = nodeName(node.tag)

    let observed: NodeState | null
    if (h.staleSec === null) {
      track.missing++
      observed = track.missing >= MISSING_HITS ? 'offline' : null
    }
    else {
      track.missing = 0
      observed = h.staleSec >= OFFLINE_SEC ? 'offline' : h.staleSec >= LAG_SEC ? 'lagging' : 'online'
    }

    if (observed === 'offline' && track.state !== 'offline') {
      track.state = 'offline'
      track.since = now
      await recordEvent({ type: 'node_offline', level: 'alarm', object, at: now, title: `${name} оффлайн`, detail: `нет метрик · ${CHECK_NOTE}` })
    }
    else if (observed === 'lagging' && track.state === 'online') {
      track.state = 'lagging'
      track.since = now
      await recordEvent({ type: 'node_lag', level: 'alarm', object, at: now, title: `${name}: метрики отстают`, detail: `нет данных ${Math.round(h.staleSec!)} с · ${CHECK_NOTE}` })
    }
    else if (observed === 'online' && track.state !== 'online') {
      const wasOffline = track.state === 'offline'
      const downMs = now - track.since
      track.state = 'online'
      track.since = now
      if (wasOffline) {
        const rebooted = h.uptimeSec !== null && h.uptimeSec < downMs / 1000 + 60
        await recordEvent({
          type: 'node_online', level: 'ok', object, at: now, title: `${name} снова в сети`,
          detail: [rebooted && 'после перезагрузки', downtimeText(downMs)].filter(Boolean).join(' · '),
        })
      }
    }

    // iowait — два превышения подряд, одна запись на эпизод.
    if (h.iowaitPct !== null && h.iowaitPct > IOWAIT_PCT) {
      track.ioHits++
      if (track.ioHits >= 2 && !track.ioAlerted) {
        track.ioAlerted = true
        await recordEvent({ type: 'node_iowait', level: 'alarm', object, at: now, title: `${name}: метрики отстают`, detail: `iowait ${Math.round(h.iowaitPct)}% · ${CHECK_NOTE}` })
      }
    }
    else {
      track.ioHits = 0
      if (h.iowaitPct !== null) track.ioAlerted = false
    }
  }
}

/** Failover-сторож увёл foreign-best на другой узел. Clash недоступен — пропуск. */
export async function checkFailover(now = Date.now()): Promise<void> {
  const proxies = await fetchProxies()
  const current = exitNodeOf(proxies.find(p => p.name === 'foreign-best')?.now)
  if (!current) return
  if (activeExit && activeExit !== current) {
    await recordEvent({
      type: 'failover', level: 'alarm', object: 'foreign-best', at: now,
      title: 'Сторож переключил экзит', detail: `${nodeName(activeExit)} → ${nodeName(current)}`,
    })
  }
  activeExit = current
}
