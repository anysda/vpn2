import { control } from './control'
import { and, eq, lt, ne, sql } from 'drizzle-orm'
import type { BatchItem } from 'drizzle-orm/batch'
import { useDb } from '../database/client'
import { collectorState, deviceTrafficHourly, devices } from '../database/schema'
import { ikev2Deltas, type SaCounters, type SaSample } from './ikev2-counters'
import { coalesceRuns } from './coalesce'
import { MSK_OFFSET_SEC, mskDayStartSec } from './msk-day'


/**
 * Накопительный сборщик трафика по всем протоколам — per-device.
 *
 * Каждый прогон снимает текущие байт-счётчики WireGuard / OpenVPN / IKEv2, считает
 * дельту относительно прошлого снимка и прибавляет её к
 * devices.rx_total / tx_total в БД. Итог переживает рестарты сервисов.
 *
 * Прошлый снимок WG/OpenVPN держится в памяти процесса (`lastSeen`). При
 * рестарте панели он теряется → первый прогон после рестарта ре-базируется
 * (дельта 0), теряя максимум один интервал данных. Снимок IKEv2 лежит в БД
 * (см. ikev2-counters.ts). Накопленные тоталы в БД сохраняются.
 *
 * Соглашение направлений: rx = загрузка клиента (download), tx = отдача
 * клиента (upload).
 */
const lastSeen = new Map<string, { rx: number, tx: number }>()

/**
 * Скользящее окно последних дельт per-device — для статуса «в сети» (трафик
 * за RECENT_WINDOW_MS). Только память: после рестарта панели окно пустое
 * один-два прогона, это допустимо.
 */
const RECENT_WINDOW_MS = 5 * 60_000
const recent = new Map<number, Array<{ at: number, bytes: number }>>()

/** Байты (rx+tx) каждого девайса за последние 5 минут. */
export function recentBytesByDevice(nowMs = Date.now()): Map<number, number> {
  const out = new Map<number, number>()
  for (const [id, list] of recent) {
    const sum = list.reduce((acc, e) => (nowMs - e.at <= RECENT_WINDOW_MS ? acc + e.bytes : acc), 0)
    if (sum > 0) out.set(id, sum)
  }
  return out
}

// История трафика хранится год (HOURLY_RETENTION_DAYS): сетка «Активность» на
// обзоре показывает суммы по суткам за всё это время. Почасовая точность нужна
// только свежим данным: ТОП и трафик клиента считают последние 7 суток МСК,
// «Клиенты онлайн» - последние 24 часа. Поэтому сутки старше ROLLUP_AFTER_DAYS
// сворачиваются в одну запись на устройство с hour = 00:00 МСК: суточные суммы
// не меняются, а база за год меньше в разы. Чистим и сворачиваем раз в час.
const HOURLY_RETENTION_SEC = (Number(process.env.HOURLY_RETENTION_DAYS) || 365) * 86400
const ROLLUP_AFTER_DAYS = 14
let lastPrunedHour = 0

/**
 * Срок хранения и свёртка старых суток. Идемпотентна: свёрнутые сутки - уже
 * одна запись на 00:00 МСК, повторный прогон их не трогает. Так же сворачивает
 * почасовую историю, накопленную до появления свёртки.
 */
async function compactTrafficHistory(nowSec: number): Promise<void> {
  const db = useDb()
  const t = deviceTrafficHourly
  const cutoff = mskDayStartSec(nowSec * 1000, ROLLUP_AFTER_DAYS)
  // Сдвиг вписан в текст запроса: параметром libsql передал бы его дробным, и
  // деление перестало бы быть целочисленным.
  const msk = sql.raw(String(MSK_OFFSET_SEC))
  const dayStart = sql<number>`((${t.hour} + ${msk}) / 86400) * 86400 - ${msk}`
  const stale = and(lt(t.hour, cutoff), ne(t.hour, dayStart))
  // Одним батчем: свёртка без удаления удвоила бы сутки, удаление без свёртки
  // потеряло бы их.
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

interface Sample {
  deviceId: number
  proto: string
  rx: number
  tx: number
}

async function sampleWg(deviceByPubkey: Map<string, number>): Promise<Sample[]> {
  try {
    const { samples } = await control<{ samples: Array<{ key: string, rx: number, tx: number }> }>('traffic.read', { protocol: 'wg' })
    return samples.flatMap(s => {
      const deviceId = deviceByPubkey.get(s.key)
      return deviceId ? [{ deviceId, proto: 'wg', rx: s.rx, tx: s.tx }] : []
    })
  }
  catch { return [] }
}

async function sampleOvpn(): Promise<Sample[]> {
  try {
    const { samples } = await control<{ samples: Array<{ device_id: number, rx: number, tx: number }> }>('traffic.read', { protocol: 'openvpn' })
    return samples.map(s => ({ deviceId: s.device_id, proto: 'ovpn', rx: s.rx, tx: s.tx }))
  }
  catch { return [] }
}

async function sampleIkev2(usernameToDevice: Map<string, number>): Promise<SaSample[] | null> {
  try {
    const { samples } = await control<{ samples: Array<{ username: string, uniqueid: string, rx: number, tx: number }> }>('traffic.read', { protocol: 'ikev2' })
    return samples.flatMap(s => {
      const deviceId = usernameToDevice.get(s.username)
      return deviceId ? [{ deviceId, uniqueid: s.uniqueid, rx: s.rx, tx: s.tx }] : []
    })
  }
  catch { return null }
}

export const collectTraffic = coalesceRuns(async (): Promise<void> => {
  const log = useLogger()
  const db = useDb()

  // pubkey → deviceId для WG, ikev2-логин → deviceId для IKEv2
  const rows = await db
    .select({ id: devices.id, pk: devices.wgPublicKey, ikev2: devices.ikev2Username })
    .from(devices)
  const deviceByPubkey = new Map<string, number>()
  const deviceByIkev2User = new Map<string, number>()
  for (const r of rows) {
    if (r.pk) deviceByPubkey.set(r.pk, r.id)
    if (r.ikev2) deviceByIkev2User.set(r.ikev2, r.id)
  }

  const [wg, ovpn, ikev2] = await Promise.all([
    sampleWg(deviceByPubkey),
    sampleOvpn(),
    sampleIkev2(deviceByIkev2User),
  ])

  // дельты по девайсу
  const deltas = new Map<number, { rx: number, tx: number }>()
  function add(deviceId: number, dRx: number, dTx: number) {
    if (dRx <= 0 && dTx <= 0) return
    const d = deltas.get(deviceId) ?? { rx: 0, tx: 0 }
    d.rx += dRx
    d.tx += dTx
    deltas.set(deviceId, d)
  }
  for (const s of [...wg, ...ovpn]) {
    const key = `${s.deviceId}:${s.proto}`
    const last = lastSeen.get(key)
    if (!last) continue // первое наблюдение — только базируемся
    // счётчик мог обнулиться (рестарт сервиса) → берём текущее значение целиком
    add(s.deviceId, s.rx >= last.rx ? s.rx - last.rx : s.rx, s.tx >= last.tx ? s.tx - last.tx : s.tx)
  }
  let ikev2Next: SaCounters | null = null
  if (ikev2) {
    const [row] = await db.select().from(collectorState).where(eq(collectorState.name, 'ikev2'))
    const res = ikev2Deltas(row ? JSON.parse(row.value) as SaCounters : null, ikev2)
    res.deltas.forEach(d => add(d.deviceId, d.rx, d.tx))
    ikev2Next = res.next
  }

  const now = Date.now()
  const hour = Math.floor(now / 3_600_000) * 3600
  // Тоталы и снимок IKEv2 — одним батчем (BEGIN..COMMIT на том же соединении):
  // упади запись между ними, следующий прогон засчитал бы тот же интервал
  // второй раз или потерял его. db.transaction у libsql на файле открывает
  // второе соединение — параллельная запись из API ловила бы SQLITE_BUSY.
  const writes: BatchItem<'sqlite'>[] = []
  for (const [id, d] of deltas) {
    const rx = Math.round(d.rx)
    const tx = Math.round(d.tx)
    writes.push(
      db
        .update(devices)
        .set({
          rxTotal: sql`${devices.rxTotal} + ${rx}`,
          txTotal: sql`${devices.txTotal} + ${tx}`,
        })
        .where(eq(devices.id, id)),
      db
        .insert(deviceTrafficHourly)
        .values({ deviceId: id, hour, rx, tx })
        .onConflictDoUpdate({
          target: [deviceTrafficHourly.deviceId, deviceTrafficHourly.hour],
          set: {
            rx: sql`${deviceTrafficHourly.rx} + ${rx}`,
            tx: sql`${deviceTrafficHourly.tx} + ${tx}`,
          },
        }),
    )
  }
  if (ikev2Next) {
    const value = JSON.stringify(ikev2Next)
    writes.push(
      db
        .insert(collectorState)
        .values({ name: 'ikev2', value })
        .onConflictDoUpdate({ target: collectorState.name, set: { value } }),
    )
  }
  const [first, ...rest] = writes
  if (first) await db.batch([first, ...rest])
  // Продвигаем снимок только после COMMIT: SQLITE_FULL/BUSY не должны
  // съедать интервал WG/OpenVPN. Прогоны последовательны через coalesceRuns.
  for (const s of [...wg, ...ovpn]) {
    lastSeen.set(`${s.deviceId}:${s.proto}`, { rx: s.rx, tx: s.tx })
  }
  for (const [id, d] of deltas) {
    const list = recent.get(id) ?? []
    list.push({ at: now, bytes: Math.round(d.rx) + Math.round(d.tx) })
    recent.set(id, list)
  }

  // Окно «в сети»: выкидываем старое и девайсы, у которых в окне ничего нет.
  for (const [id, list] of recent) {
    const fresh = list.filter(e => now - e.at <= RECENT_WINDOW_MS)
    if (fresh.length) recent.set(id, fresh)
    else recent.delete(id)
  }

  if (hour !== lastPrunedHour) {
    lastPrunedHour = hour
    await compactTrafficHistory(Math.floor(now / 1000))
  }

  log.info(
    { wg: wg.length, ovpn: ovpn.length, ikev2: ikev2?.length ?? null, updated: deltas.size },
    'traffic: collected',
  )
})
