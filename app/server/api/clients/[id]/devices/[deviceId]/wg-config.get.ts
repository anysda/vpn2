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

  setHeader(event, 'content-type', 'text/plain; charset=utf-8')
  return conf
})
