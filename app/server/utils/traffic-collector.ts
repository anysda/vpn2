import { execFile } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { promisify } from 'node:util'
import { eq, lt, sql } from 'drizzle-orm'
import { useDb } from '../database/client'
import { deviceTrafficHourly, devices } from '../database/schema'

const execFileP = promisify(execFile)

/**
 * Накопительный сборщик трафика по всем протоколам — per-device.
 *
 * Каждый прогон снимает текущие байт-счётчики WireGuard / OpenVPN / IKEv2, считает
 * дельту относительно прошлого снимка и прибавляет её к
 * devices.rx_total / tx_total в БД. Итог переживает рестарты сервисов.
 *
 * Прошлый снимок держится в памяти процесса (`lastSeen`). При рестарте
 * панели он теряется → первый прогон после рестарта ре-базируется (дельта 0),
 * теряя максимум один интервал данных. Накопленные тоталы в БД сохраняются.
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

// Почасовая история хранится 35 дней: хватает на неделю с запасом и на
// месячные отчёты, если понадобятся. Чистим раз в час.
const HOURLY_RETENTION_SEC = 35 * 86400
let lastPrunedHour = 0

interface Sample {
  deviceId: number
  proto: string
  rx: number
  tx: number
  /**
   * Счётчик источника заведомо начинается с нуля при появлении ключа
   * (у IKEv2 — новая SA). Тогда первое наблюдение можно засчитать целиком,
   * а не терять интервал на ре-базирование.
   */
  baseZero?: boolean
}

/** WireGuard: `wg show wg0 transfer` → "<pubkey>\t<rx>\t<tx>" (со стороны сервера). */
async function sampleWg(deviceByPubkey: Map<string, number>): Promise<Sample[]> {
  let out: string
  try {
    out = (await execFileP('wg', ['show', 'wg0', 'transfer'], { timeout: 4000 })).stdout
  }
  catch {
    return []
  }
  const samples: Sample[] = []
  for (const line of out.trim().split('\n')) {
    const [pk, rx, tx] = line.split('\t')
    const id = pk ? deviceByPubkey.get(pk) : undefined
    if (!id) continue
    // wg «received» = принято от пира = upload клиента (tx);
    // wg «sent» = отдано пиру = download клиента (rx).
    samples.push({ deviceId: id, proto: 'wg', rx: Number(tx) || 0, tx: Number(rx) || 0 })
  }
  return samples
}

/** OpenVPN: status-version 2 файл. CLIENT_LIST,CN,real,vaddr,v6,bytesRecv,bytesSent,... */
async function sampleOvpn(): Promise<Sample[]> {
  let text: string
  try {
    text = await readFile('/etc/openvpn/server/status-server.log', 'utf8')
  }
  catch {
    return []
  }
  const samples: Sample[] = []
  for (const line of text.split('\n')) {
    if (!line.startsWith('CLIENT_LIST,')) continue
    const f = line.split(',')
    const cn = f[1]?.match(/^dev_(\d+)$/)
    if (!cn) continue
    const bytesRecv = Number(f[5]) || 0 // принято сервером от клиента = upload (tx)
    const bytesSent = Number(f[6]) || 0 // отдано сервером клиенту = download (rx)
    samples.push({ deviceId: Number(cn[1]), proto: 'ovpn', rx: bytesSent, tx: bytesRecv })
  }
  return samples
}

/**
 * IKEv2: /etc/anysda/ikev2-status.json — его каждые 10 с пишет хостовый
 * anysda-ikev2-sync (стадия 27-ikev2, шаг 8) по данным vici. Внутри
 * контейнера панели swanctl'а нет, поэтому счётчики приходят файлом — ровно
 * как status-файл OpenVPN.
 *
 * Ключ дельты — per-SA (proto = «ikev2:<uniqueid>»), а не per-device: при
 * переподключении charon заводит новую SA со счётчиками с нуля, и общая сумма
 * по устройству просела бы, что выглядело бы как сброс счётчика.
 */
async function sampleIkev2(usernameToDevice: Map<string, number>): Promise<Sample[]> {
  let raw: string
  try {
    raw = await readFile('/etc/anysda/ikev2-status.json', 'utf8')
  }
  catch {
    return []
  }
  let parsed: { sessions?: Array<{ uniqueid?: string, username?: string, rx?: number, tx?: number }> }
  try {
    parsed = JSON.parse(raw)
  }
  catch {
    return []
  }
  const samples: Sample[] = []
  for (const s of parsed.sessions ?? []) {
    const id = s.username ? usernameToDevice.get(s.username) : undefined
    if (!id) continue
    // Скрипт уже привёл направления к соглашению панели: rx = скачано
    // клиентом (bytes-out сервера), tx = отдано клиентом (bytes-in).
    samples.push({
      deviceId: id,
      proto: `ikev2:${s.uniqueid ?? '0'}`,
      rx: Number(s.rx) || 0,
      tx: Number(s.tx) || 0,
      baseZero: true,
    })
  }
  return samples
}

export async function collectTraffic(): Promise<void> {
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
  const samples = [...wg, ...ovpn, ...ikev2]

  // дельты по девайсу
  const deltas = new Map<number, { rx: number, tx: number }>()
  for (const s of samples) {
    const key = `${s.deviceId}:${s.proto}`
    const last = lastSeen.get(key) ?? (s.baseZero ? { rx: 0, tx: 0 } : undefined)
    lastSeen.set(key, { rx: s.rx, tx: s.tx })
    if (!last) continue // первое наблюдение — только базируемся
    // счётчик мог обнулиться (рестарт сервиса) → берём текущее значение целиком
    const dRx = s.rx >= last.rx ? s.rx - last.rx : s.rx
    const dTx = s.tx >= last.tx ? s.tx - last.tx : s.tx
    if (dRx <= 0 && dTx <= 0) continue
    const d = deltas.get(s.deviceId) ?? { rx: 0, tx: 0 }
    d.rx += dRx
    d.tx += dTx
    deltas.set(s.deviceId, d)
  }

  const now = Date.now()
  const hour = Math.floor(now / 3_600_000) * 3600
  for (const [id, d] of deltas) {
    const rx = Math.round(d.rx)
    const tx = Math.round(d.tx)
    await db
      .update(devices)
      .set({
        rxTotal: sql`${devices.rxTotal} + ${rx}`,
        txTotal: sql`${devices.txTotal} + ${tx}`,
      })
      .where(eq(devices.id, id))
    await db
      .insert(deviceTrafficHourly)
      .values({ deviceId: id, hour, rx, tx })
      .onConflictDoUpdate({
        target: [deviceTrafficHourly.deviceId, deviceTrafficHourly.hour],
        set: {
          rx: sql`${deviceTrafficHourly.rx} + ${rx}`,
          tx: sql`${deviceTrafficHourly.tx} + ${tx}`,
        },
      })
    const list = recent.get(id) ?? []
    list.push({ at: now, bytes: rx + tx })
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
    await db
      .delete(deviceTrafficHourly)
      .where(lt(deviceTrafficHourly.hour, hour - HOURLY_RETENTION_SEC))
  }

  log.info(
    { wg: wg.length, ovpn: ovpn.length, ikev2: ikev2.length, updated: deltas.size },
    'traffic: collected',
  )
}
