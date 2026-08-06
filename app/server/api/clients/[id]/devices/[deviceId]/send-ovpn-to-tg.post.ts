import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../../database/client'
import { clients, devices } from '../../../../../database/schema'
import { requireAuth } from '../../../../../utils/auth'
import { clientDns } from '../../../../../utils/client-dns'
import { deviceConfigName } from '../../../../../utils/naming'
import { buildOvpnConfig, caReady, ensureDeviceOvpn } from '../../../../../utils/openvpn'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!Number.isFinite(clientId) || !Number.isFinite(deviceId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const cfg = useRuntimeConfig()
  if (!cfg.ovpnEnabled) throw createError({ statusCode: 503, statusMessage: 'OpenVPN выключен' })
  const endpoint = String(cfg.ovpnPublicHost || '')
  if (!endpoint) throw createError({ statusCode: 500, statusMessage: 'ovpn_public_host_not_configured' })

  const secret = String(cfg.tgbotSecret ?? '')
  if (!secret) throw createError({ statusCode: 503, statusMessage: 'Telegram-бот не настроен' })

  if (!(await caReady())) {
    throw createError({ statusCode: 503, statusMessage: 'OpenVPN CA ещё не инициализирован (29-openvpn)' })
  }

  const db = useDb()
  const [client] = await db.select().from(clients).where(eq(clients.id, clientId)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  const [device] = await db.select().from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, clientId)))
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

  const name = deviceConfigName(client.name, device.name)
  const port = Number(cfg.tgbotEventPort ?? 8877)
  try {
    await $fetch(`http://127.0.0.1:${port}/event`, {
      method: 'POST',
      headers: { 'X-Tgbot-Secret': secret },
      body: { type: 'client_send_openvpn', name, conf },
      timeout: 8000,
    })
  }
  catch (e) {
    throw createError({
      statusCode: 502,
      statusMessage: 'Бот не отвечает — проверь /api/admin/telegram',
      cause: e,
    })
  }

  return { ok: true }
})
