import { useDb } from '../database/client'
import { devices } from '../database/schema'
import { sampleIkev2, sampleOvpn, sampleWg } from './traffic-collector'

/**
 * Текущая скорость клиентов (байт/с) по накопительным счётчикам WireGuard,
 * OpenVPN и IKEv2 — тем же чтением, что у минутного сборщика трафика.
 *
 * Скорость по каждому счётчику считается по СМЕНЕ его значения: у IKEv2 файл
 * статуса обновляется раз в 10 с, и дельта на каждом 3-секундном опросе давала
 * бы «0 → пик». Сглаживание EWMA (TAU_MS) — чтобы список клиентов, который
 * сортируется по скорости, не прыгал на каждом опросе. Счётчик, не менявшийся
 * дольше STALE_MS, считается простаивающим (скорость 0). Сброс счётчика
 * (рестарт службы) — новая база без скачка.
 *
 * Снимок не чаще MIN_INTERVAL_MS: несколько открытых вкладок панели не гоняют
 * `wg show` пачкой.
 */
const MIN_INTERVAL_MS = 2_000
const TAU_MS = 6_000
const STALE_MS = 12_000

interface CounterState { rx: number, tx: number, at: number, rxBps: number, txBps: number, deviceId: number }
const counters = new Map<string, CounterState>()
let cache: { at: number, byClient: Map<number, { rxBps: number, txBps: number }> } | null = null

function observe(key: string, deviceId: number, rx: number, tx: number, now: number) {
  const st = counters.get(key)
  if (!st || rx < st.rx || tx < st.tx) {
    counters.set(key, { rx, tx, at: now, rxBps: 0, txBps: 0, deviceId })
    return
  }
  if (rx === st.rx && tx === st.tx) return
  const dt = now - st.at
  if (dt <= 0) return
  const k = 1 - Math.exp(-dt / TAU_MS)
  st.rxBps += ((rx - st.rx) * 1000 / dt - st.rxBps) * k
  st.txBps += ((tx - st.tx) * 1000 / dt - st.txBps) * k
  st.rx = rx
  st.tx = tx
  st.at = now
}

/** Текущая скорость по клиентам: clientId → { rxBps, txBps } (download/upload клиента). */
export async function currentRatesByClient(): Promise<Map<number, { rxBps: number, txBps: number }>> {
  const now = Date.now()
  if (cache && now - cache.at < MIN_INTERVAL_MS) return cache.byClient

  const rows = await useDb()
    .select({ id: devices.id, clientId: devices.clientId, pk: devices.wgPublicKey, ikev2: devices.ikev2Username })
    .from(devices)
  const byPubkey = new Map<string, number>()
  const byIkev2User = new Map<string, number>()
  const clientOf = new Map<number, number>()
  for (const r of rows) {
    clientOf.set(r.id, r.clientId)
    if (r.pk) byPubkey.set(r.pk, r.id)
    if (r.ikev2) byIkev2User.set(r.ikev2, r.id)
  }

  const [wg, ovpn, ikev2] = await Promise.all([sampleWg(byPubkey), sampleOvpn(), sampleIkev2(byIkev2User)])
  const seen = new Set<string>()
  for (const s of [...wg, ...ovpn]) {
    const key = `${s.deviceId}:${s.proto}`
    seen.add(key)
    observe(key, s.deviceId, s.rx, s.tx, now)
  }
  for (const s of ikev2 ?? []) {
    const key = `${s.deviceId}:ikev2:${s.uniqueid}`
    seen.add(key)
    observe(key, s.deviceId, s.rx, s.tx, now)
  }
  for (const key of counters.keys()) {
    if (!seen.has(key)) counters.delete(key) // устройство отключилось / SA закрыта
  }

  const byClient = new Map<number, { rxBps: number, txBps: number }>()
  for (const st of counters.values()) {
    if (now - st.at > STALE_MS) continue
    const clientId = clientOf.get(st.deviceId)
    if (clientId == null) continue
    const c = byClient.get(clientId) ?? { rxBps: 0, txBps: 0 }
    c.rxBps += Math.max(0, st.rxBps)
    c.txBps += Math.max(0, st.txBps)
    byClient.set(clientId, c)
  }
  cache = { at: now, byClient }
  return byClient
}
