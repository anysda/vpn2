import { execFile } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { promisify } from 'node:util'
import { eq, sql } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients } from '../database/schema'

const execFileP = promisify(execFile)

/**
 * Накопительный сборщик трафика по всем протоколам.
 *
 * Каждый прогон снимает текущие байт-счётчики SS / WireGuard / OpenVPN,
 * считает дельту относительно прошлого снимка и прибавляет её к
 * clients.rx_total / tx_total в БД. Итог переживает рестарты сервисов.
 *
 * Прошлый снимок держится в памяти процесса (`lastSeen`). При рестарте
 * панели он теряется → первый прогон после рестарта ре-базируется (дельта 0),
 * теряя максимум один интервал данных. Накопленные тоталы в БД сохраняются.
 *
 * Соглашение направлений: rx = загрузка клиента (download), tx = отдача
 * клиента (upload) — как в shadowsocks-метриках (p>c / c>p).
 */
const lastSeen = new Map<string, { rx: number, tx: number }>()

interface Sample { clientId: number, proto: string, rx: number, tx: number }

/** SS: outline-ss-server Prometheus. dir c>p = upload(tx), p>c = download(rx). */
async function sampleSs(): Promise<Sample[]> {
  const url = String(useRuntimeConfig().ssPrometheusUrl ?? 'http://127.0.0.1:9091')
  let text: string
  try {
    text = await $fetch<string>(`${url}/metrics`, { timeout: 2500, responseType: 'text' })
  }
  catch {
    return []
  }
  const acc = new Map<number, { rx: number, tx: number }>()
  const re = /shadowsocks_data_bytes(?:_per_location)?\{([^}]+)\}\s+([0-9eE.+-]+)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    const value = Number(m[2])
    if (!Number.isFinite(value)) continue
    const ak = m[1]!.match(/access_key="client_(\d+)"/)
    const dir = m[1]!.match(/dir="([^"]+)"/)
    if (!ak || !dir) continue
    const id = Number(ak[1])
    const slot = acc.get(id) ?? { rx: 0, tx: 0 }
    if (dir[1] === 'c>p') slot.tx += value
    else if (dir[1] === 'p>c') slot.rx += value
    acc.set(id, slot)
  }
  return [...acc].map(([clientId, v]) => ({ clientId, proto: 'ss', rx: v.rx, tx: v.tx }))
}

/** WireGuard: `wg show wg0 transfer` → "<pubkey>\t<rx>\t<tx>" (со стороны сервера). */
async function sampleWg(clientByPubkey: Map<string, number>): Promise<Sample[]> {
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
    const id = pk ? clientByPubkey.get(pk) : undefined
    if (!id) continue
    // wg «received» = принято от пира = upload клиента (tx);
    // wg «sent» = отдано пиру = download клиента (rx).
    samples.push({ clientId: id, proto: 'wg', rx: Number(tx) || 0, tx: Number(rx) || 0 })
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
    const cn = f[1]?.match(/^client_(\d+)$/)
    if (!cn) continue
    const bytesRecv = Number(f[5]) || 0 // принято сервером от клиента = upload (tx)
    const bytesSent = Number(f[6]) || 0 // отдано сервером клиенту = download (rx)
    samples.push({ clientId: Number(cn[1]), proto: 'ovpn', rx: bytesSent, tx: bytesRecv })
  }
  return samples
}

export async function collectTraffic(): Promise<void> {
  const db = useDb()

  // pubkey → clientId для WG
  const rows = await db
    .select({ id: clients.id, pk: clients.wgPublicKey })
    .from(clients)
  const clientByPubkey = new Map<string, number>()
  for (const r of rows) if (r.pk) clientByPubkey.set(r.pk, r.id)

  const samples = (await Promise.all([
    sampleSs(),
    sampleWg(clientByPubkey),
    sampleOvpn(),
  ])).flat()

  // дельты по клиенту
  const deltas = new Map<number, { rx: number, tx: number }>()
  for (const s of samples) {
    const key = `${s.clientId}:${s.proto}`
    const last = lastSeen.get(key)
    lastSeen.set(key, { rx: s.rx, tx: s.tx })
    if (!last) continue // первое наблюдение — только базируемся
    // счётчик мог обнулиться (рестарт сервиса) → берём текущее значение целиком
    const dRx = s.rx >= last.rx ? s.rx - last.rx : s.rx
    const dTx = s.tx >= last.tx ? s.tx - last.tx : s.tx
    if (dRx <= 0 && dTx <= 0) continue
    const d = deltas.get(s.clientId) ?? { rx: 0, tx: 0 }
    d.rx += dRx
    d.tx += dTx
    deltas.set(s.clientId, d)
  }

  for (const [id, d] of deltas) {
    await db
      .update(clients)
      .set({
        rxTotal: sql`${clients.rxTotal} + ${Math.round(d.rx)}`,
        txTotal: sql`${clients.txTotal} + ${Math.round(d.tx)}`,
      })
      .where(eq(clients.id, id))
  }
}
