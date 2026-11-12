import { and, eq, lt, ne, sql, gte } from 'drizzle-orm'
import type { BatchItem } from 'drizzle-orm/batch'
import { useDb } from '../database/client'
import { collectorState, deviceTrafficHourly, devices } from '../database/schema'

// Сборщик: каждый прогон прибавляет дельты счётчиков WG, OpenVPN и IKEv2 к devices.rx_total/tx_total
// и почасовой истории; rx - загрузка клиента, tx - отдача. Прошлый снимок WG/OpenVPN - в памяти
// (после рестарта первый прогон только базируется), IKEv2 - в базе: счётчик живёт, пока жива IKE_SA.
const lastSeen = new Map<string, { rx: number, tx: number }>()

const RECENT_WINDOW_MS = 5 * 60_000
const recent = new Map<number, Array<{ at: number, bytes: number }>>()

export function recentBytesByDevice(nowMs = Date.now()): Map<number, number> {
  const out = new Map<number, number>()
  for (const [id, list] of recent) {
    const sum = list.reduce((acc, e) => (nowMs - e.at <= RECENT_WINDOW_MS ? acc + e.bytes : acc), 0)
    if (sum > 0) out.set(id, sum)
  }
  return out
}

const HOURLY_RETENTION_SEC = (Number(process.env.HOURLY_RETENTION_DAYS) || 365) * 86400
const ROLLUP_AFTER_DAYS = 14
let lastPrunedHour = 0

/** Срок хранения и свёртка суток старше двух недель в одну запись на 00:00 МСК, одним батчем. */
async function compactTrafficHistory(nowSec: number): Promise<void> {
  const db = useDb()
  const t = deviceTrafficHourly
  const cutoff = mskDayStartSec(nowSec * 1000, ROLLUP_AFTER_DAYS)
  // Сдвиг вписан в текст запроса: параметром libsql передал бы его дробным.
  const msk = sql.raw(String(MSK_OFFSET_SEC))
  const dayStart = sql<number>`((${t.hour} + ${msk}) / 86400) * 86400 - ${msk}`
  const stale = and(lt(t.hour, cutoff), ne(t.hour, dayStart))
  await db.batch([
    db.delete(t).where(lt(t.hour, Math.floor(nowSec / 3600) * 3600 - HOURLY_RETENTION_SEC)),
    db
      .insert(t)
      .select(db
        .select({ deviceId: t.deviceId, hour: dayStart.as('day_start'), rx: sql<number>`sum(${t.rx})`.as('rx'), tx: sql<number>`sum(${t.tx})`.as('tx') })
        .from(t)
        .where(stale)
        .groupBy(t.deviceId, dayStart))
      .onConflictDoUpdate({
        target: [t.deviceId, t.hour],
        set: { rx: sql`${t.rx} + excluded.rx`, tx: sql`${t.tx} + excluded.tx` },
      }),
    db.delete(t).where(stale),
  ])
}

type Counter = { rx: number, tx: number }
async function sample<T>(protocol: string, pick: (s: T & Counter) => { deviceId?: number, uniqueid?: string }) {
  try {
    const { samples } = await control<{ samples: Array<T & Counter> }>('traffic.read', { protocol })
    return samples.flatMap((s) => {
      const { deviceId, uniqueid = protocol } = pick(s)
      return deviceId ? [{ deviceId, uniqueid, rx: s.rx, tx: s.tx }] : []
    })
  }
  catch { return null }
}

export const collectTraffic = coalesceRuns(async (): Promise<void> => {
  const log = useLogger()
  const db = useDb()

  const rows = await db.select({ id: devices.id, pk: devices.wgPublicKey, ikev2: devices.ikev2Username }).from(devices)
  const deviceByPubkey = new Map<string, number>()
  const deviceByIkev2User = new Map<string, number>()
  for (const r of rows) {
    if (r.pk) deviceByPubkey.set(r.pk, r.id)
    if (r.ikev2) deviceByIkev2User.set(r.ikev2, r.id)
  }
  const [wgSamples, ovpnSamples, ikev2] = await Promise.all([
    sample<{ key: string }>('wg', s => ({ deviceId: deviceByPubkey.get(s.key) })),
    sample<{ device_id: number }>('openvpn', s => ({ deviceId: s.device_id })),
    sample<{ username: string, uniqueid: string }>('ikev2', s => ({ deviceId: deviceByIkev2User.get(s.username), uniqueid: s.uniqueid })),
  ])
  const wg = wgSamples ?? []
  const ovpn = ovpnSamples ?? []

  const deltas = new Map<number, { rx: number, tx: number }>()
  function add(deviceId: number, dRx: number, dTx: number) {
    if (dRx <= 0 && dTx <= 0) return
    const d = deltas.get(deviceId) ?? { rx: 0, tx: 0 }
    d.rx += dRx
    d.tx += dTx
    deltas.set(deviceId, d)
  }
  for (const s of [...wg, ...ovpn]) {
    const last = lastSeen.get(`${s.deviceId}:${s.uniqueid}`)
    if (!last) continue
    // Меньше прошлого - счётчик обнулился (рестарт службы): берём текущее целиком.
    add(s.deviceId, s.rx >= last.rx ? s.rx - last.rx : s.rx, s.tx >= last.tx ? s.tx - last.tx : s.tx)
  }
  // IKE_SA по «<deviceId>:<uniqueid>»: новая засчитывается целиком, исчезнувшие из снимка уходят.
  let ikev2Next: Record<string, [number, number]> | null = null
  if (ikev2) {
    const [row] = await db.select().from(collectorState).where(eq(collectorState.name, 'ikev2'))
    const prev: Record<string, [number, number]> | null = row ? JSON.parse(row.value) : null
    ikev2Next = {}
    for (const s of ikev2) {
      const key = `${s.deviceId}:${s.uniqueid}`
      ikev2Next[key] = [s.rx, s.tx]
      if (!prev) continue
      const [lastRx, lastTx] = prev[key] ?? [0, 0]
      add(s.deviceId, s.rx >= lastRx ? s.rx - lastRx : s.rx, s.tx >= lastTx ? s.tx - lastTx : s.tx)
    }
  }

  const now = Date.now()
  const hour = Math.floor(now / 3_600_000) * 3600
  // Тоталы и снимок IKEv2 одним батчем: сбой между ними засчитал бы интервал дважды или потерял его.
  const writes: BatchItem<'sqlite'>[] = []
  const hourly = deviceTrafficHourly
  for (const [id, d] of deltas) {
    const rx = Math.round(d.rx)
    const tx = Math.round(d.tx)
    writes.push(
      db.update(devices).set({ rxTotal: sql`${devices.rxTotal} + ${rx}`, txTotal: sql`${devices.txTotal} + ${tx}` }).where(eq(devices.id, id)),
      db.insert(hourly).values({ deviceId: id, hour, rx, tx }).onConflictDoUpdate({
        target: [hourly.deviceId, hourly.hour],
        set: { rx: sql`${hourly.rx} + ${rx}`, tx: sql`${hourly.tx} + ${tx}` },
      }),
    )
  }
  if (ikev2Next) {
    const value = JSON.stringify(ikev2Next)
    writes.push(db.insert(collectorState).values({ name: 'ikev2', value }).onConflictDoUpdate({ target: collectorState.name, set: { value } }))
  }
  const [first, ...rest] = writes
  if (first) await db.batch([first, ...rest])
  for (const s of [...wg, ...ovpn]) lastSeen.set(`${s.deviceId}:${s.uniqueid}`, { rx: s.rx, tx: s.tx })
  for (const [id, d] of deltas) {
    const list = recent.get(id) ?? []
    list.push({ at: now, bytes: Math.round(d.rx) + Math.round(d.tx) })
    recent.set(id, list)
  }

  for (const [id, list] of recent) {
    const fresh = list.filter(e => now - e.at <= RECENT_WINDOW_MS)
    if (fresh.length) recent.set(id, fresh)
    else recent.delete(id)
  }

  if (hour !== lastPrunedHour) {
    lastPrunedHour = hour
    await compactTrafficHistory(Math.floor(now / 1000))
  }

  log.info({ wg: wg.length, ovpn: ovpn.length, ikev2: ikev2?.length ?? null, updated: deltas.size }, 'traffic: collected')
})

// «В сети» - от 20 КБ за 5 минут (keepalive и рукопожатия простаивающего устройства ~0,5 КБ),
// «активен за сутки/неделю» - от 1 МБ за период.
export const ONLINE_MIN_BYTES = 20 * 1024

export const PERIOD_ACTIVE_MIN_BYTES = 1024 ** 2

export function periodStartSec(period: 'day' | 'week', nowMs = Date.now()): number {
  return mskDayStartSec(nowMs, period === 'week' ? 6 : 0)
}

export async function periodTotalsByClient(sinceSec: number): Promise<Map<number, { rx: number, tx: number }>> {
  const rows = await useDb()
    .select({
      clientId: devices.clientId,
      rx: sql<number>`coalesce(sum(${deviceTrafficHourly.rx}), 0)`,
      tx: sql<number>`coalesce(sum(${deviceTrafficHourly.tx}), 0)`,
    })
    .from(deviceTrafficHourly)
    .innerJoin(devices, eq(devices.id, deviceTrafficHourly.deviceId))
    .where(gte(deviceTrafficHourly.hour, sinceSec))
    .groupBy(devices.clientId)
  return new Map(rows.map(r => [r.clientId, { rx: Number(r.rx), tx: Number(r.tx) }]))
}

export async function recentBytesByClient(): Promise<Map<number, number>> {
  const byDevice = recentBytesByDevice()
  const out = new Map<number, number>()
  if (!byDevice.size) return out
  const rows = await useDb().select({ id: devices.id, clientId: devices.clientId }).from(devices)
  for (const r of rows) {
    const b = byDevice.get(r.id)
    if (b) out.set(r.clientId, (out.get(r.clientId) ?? 0) + b)
  }
  return out
}

export async function historyStartSec(): Promise<number | null> {
  const [row] = await useDb()
    .select({ first: sql<number | null>`min(${deviceTrafficHourly.hour})` })
    .from(deviceTrafficHourly)
  return row?.first == null ? null : Number(row.first)
}

/** Активность по суткам МСК за всю историю: клиентов от PERIOD_ACTIVE_MIN_BYTES и байты; сутки без записей не выдумываем. */
export async function activityDays(nowMs = Date.now()) {
  const day = sql<number>`cast((${deviceTrafficHourly.hour} + ${MSK_OFFSET_SEC}) / 86400 as integer)`
  const rows = await useDb()
    .select({ day, bytes: sql<number>`sum(${deviceTrafficHourly.rx} + ${deviceTrafficHourly.tx})` })
    .from(deviceTrafficHourly)
    .innerJoin(devices, eq(devices.id, deviceTrafficHourly.deviceId))
    .groupBy(day, devices.clientId)
  const days = new Map<number, { day: number, online: number, bytes: number }>()
  for (const r of rows) {
    const d = days.get(Number(r.day)) ?? { day: Number(r.day), online: 0, bytes: 0 }
    d.bytes += Number(r.bytes)
    if (Number(r.bytes) >= PERIOD_ACTIVE_MIN_BYTES) d.online++
    days.set(d.day, d)
  }
  return { today: mskDay(nowMs), days: [...days.values()].sort((a, b) => a.day - b.day) }
}
