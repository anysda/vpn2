import QRCode from 'qrcode-svg'
import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../../database/client'
import { clients, devices } from '../../../../../database/schema'
import { requireAuth } from '../../../../../utils/auth'
import { clientDns } from '../../../../../utils/client-dns'
import { buildWgClientConfig, ensureDeviceWg, loadServerKeys, syncWireguardConfig } from '../../../../../utils/wireguard'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!Number.isFinite(clientId) || !Number.isFinite(deviceId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const cfg = useRuntimeConfig()
  if (!cfg.wgEnabled) throw createError({ statusCode: 503, statusMessage: 'WireGuard выключен' })
  const endpoint = String(cfg.wgPublicHost || '')
  if (!endpoint) throw createError({ statusCode: 500, statusMessage: 'wg_public_host_not_configured' })

  const db = useDb()
  const [client] = await db.select().from(clients).where(eq(clients.id, clientId)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  const [device] = await db.select().from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, clientId)))
    .limit(1)
  if (!device) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  const server = await loadServerKeys().catch(() => null)
  if (!server) {
    throw createError({ statusCode: 503, statusMessage: 'WireGuard server-ключи ещё не инициализированы (28-wireguard)' })
  }

  const dev = await ensureDeviceWg(deviceId)
  await syncWireguardConfig().catch(() => {})

  const conf = buildWgClientConfig({
    clientPrivateKey: dev.wgPrivateKey!,
    clientPresharedKey: dev.wgPresharedKey!,
    clientIp: dev.wgIp!,
    serverPublicKey: server.publicKey,
    serverEndpoint: endpoint,
    serverPort: Number(cfg.wgListenPort),
    dns: clientDns(client.filterTraffic).join(', '),
    mtu: Number(cfg.wgMtu),
    splitLocal: cfg.wgSplitLocal !== false,
    tunnelPrefixes: [String(cfg.wgSubnetPrefix), String(cfg.mgmtMeshIpPrefix)],
  })

  // Сплит-туннель раздувает конфиг с ~330 до ~950 байт (42 префикса в
  // AllowedIPs), поэтому ecl 'L': при 'M' QR получается настолько плотным,
  // что телефон его с экрана уже не берёт.
  setHeader(event, 'content-type', 'image/svg+xml; charset=utf-8')
  return qrSvg(conf)
})

/**
 * Свой вывод вместо `.svg()` библиотеки: та рисует отдельный <rect> со стилем
 * на каждый модуль и с дробными координатами (512px / N модулей), и на таком
 * конфиге выходил мегабайт. Здесь один path в единицах модуля: подряд идущие
 * тёмные модули строки сливаются в один отрезок, размер задаёт CSS через viewBox.
 */
function qrSvg(content: string): string {
  const pad = 2
  const qr = new QRCode({ content, padding: 0, ecl: 'L' }) as unknown as {
    qrcode: { modules: boolean[][] }
  }
  const m = qr.qrcode.modules
  const n = m.length
  let d = ''
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      if (!m[x]![y]) continue
      const x0 = x
      while (x + 1 < n && m[x + 1]![y]) x++
      d += `M${x0 + pad} ${y + pad}h${x - x0 + 1}v1h-${x - x0 + 1}z`
    }
  }
  const size = n + 2 * pad
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" shape-rendering="crispEdges">`
    + `<rect width="${size}" height="${size}" fill="#fff"/><path d="${d}"/></svg>`
}
