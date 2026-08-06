import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../database/client'
import { devices } from '../../../../database/schema'
import { clientByChat, requireBotAuth } from '../../../../utils/bot-api'
import { clientDns } from '../../../../utils/client-dns'
import { buildOvpnConfig, caReady, ensureDeviceOvpn } from '../../../../utils/openvpn'

// Выдача OpenVPN-конфига девайса по запросу клиента из бота.
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  const chatId = Number(getQuery(event).chatId)
  if (!Number.isFinite(deviceId) || !Number.isFinite(chatId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_params' })
  }

  const cfg = useRuntimeConfig()
  if (!cfg.ovpnEnabled) throw createError({ statusCode: 503, statusMessage: 'OpenVPN выключен' })
  const endpoint = String(cfg.ovpnPublicHost || '')
  if (!endpoint) throw createError({ statusCode: 500, statusMessage: 'ovpn_public_host_not_configured' })
  if (!(await caReady())) throw createError({ statusCode: 503, statusMessage: 'OpenVPN CA не готов' })

  const client = await clientByChat(chatId)
  const db = useDb()
  const [device] = await db.select().from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, client.id)))
    .limit(1)
  if (!device) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  const dev = await ensureDeviceOvpn(deviceId)
  const conf = await buildOvpnConfig({
    clientCert: dev.ovpnCert!,
    clientKey: dev.ovpnKey!,
    serverHost: endpoint,
    serverPort: Number(cfg.ovpnPort),
    proto: String(cfg.ovpnProto),
    dns: clientDns(client.filterTraffic),
    splitLocal: cfg.wgSplitLocal !== false,
    tunnelPrefixes: [String(cfg.mgmtMeshIpPrefix)],
  })

  setHeader(event, 'content-type', 'text/plain; charset=utf-8')
  return conf
})
