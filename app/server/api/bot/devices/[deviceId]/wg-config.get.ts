import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../database/client'
import { devices } from '../../../../database/schema'
import { clientByChat, requireBotAuth } from '../../../../utils/bot-api'
import { clientDns } from '../../../../utils/client-dns'
import { buildWgClientConfig, ensureDeviceWg, loadServerKeys, syncWireguardConfig } from '../../../../utils/wireguard'

// Выдача WireGuard-конфига девайса по запросу клиента из бота.
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  const chatId = Number(getQuery(event).chatId)
  if (!Number.isFinite(deviceId) || !Number.isFinite(chatId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_params' })
  }

  const cfg = useRuntimeConfig()
  if (!cfg.wgEnabled) throw createError({ statusCode: 503, statusMessage: 'WireGuard выключен' })
  const endpoint = String(cfg.wgPublicHost || '')
  if (!endpoint) throw createError({ statusCode: 500, statusMessage: 'wg_public_host_not_configured' })

  const client = await clientByChat(chatId)
  const db = useDb()
  const [device] = await db.select().from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, client.id)))
    .limit(1)
  if (!device) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  const server = await loadServerKeys().catch(() => null)
  if (!server) throw createError({ statusCode: 503, statusMessage: 'WireGuard server-ключи не готовы' })

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

  setHeader(event, 'content-type', 'text/plain; charset=utf-8')
  return conf
})
