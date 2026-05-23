import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../../database/client'
import { clients, devices } from '../../../../../database/schema'
import { requireAuth } from '../../../../../utils/auth'
import { ikev2CaReady, renderClientMobileconfig } from '../../../../../utils/ikev2'

/**
 * Apple .mobileconfig (XML) с зашитыми Server/Username/Password + CA cert
 * (PayloadType: root → trust на anysda CA, и vpn.managed → IKEv2 профиль).
 * Импорт в один тык на iOS/macOS.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!Number.isFinite(clientId) || !Number.isFinite(deviceId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  if (!(await ikev2CaReady())) {
    throw createError({ statusCode: 503, statusMessage: 'IKEv2 CA ещё не инициализирован (стадия 27-ikev2)' })
  }

  const cfg = useRuntimeConfig()
  const serverIp = String(process.env.ENTRY_HOST || cfg.wgPublicHost || '').trim()
  if (!serverIp) {
    throw createError({ statusCode: 500, statusMessage: 'entry_host_not_configured' })
  }

  const db = useDb()
  const [client] = await db.select().from(clients).where(eq(clients.id, clientId)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  const [device] = await db.select().from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, clientId)))
    .limit(1)
  if (!device) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  const xml = await renderClientMobileconfig(deviceId, serverIp)
  setHeader(event, 'content-type', 'application/x-apple-aspen-config')
  setHeader(event, 'content-disposition',
    `attachment; filename="anysda-ikev2-${device.ikev2Username || deviceId}.mobileconfig"`)
  return xml
})
