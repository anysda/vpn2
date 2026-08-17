import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../../database/client'
import { clients, devices } from '../../../../../database/schema'
import { requireAuth } from '../../../../../utils/auth'
import { buildIkev2ClientInfo, ikev2ServerReady } from '../../../../../utils/ikev2'

/**
 * Возвращает JSON с server/username/password/remoteId + CA cert (PEM).
 * Эти поля UI выводит копируемыми; .mobileconfig — отдельный endpoint.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!Number.isFinite(clientId) || !Number.isFinite(deviceId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  if (!(await ikev2ServerReady())) {
    throw createError({ statusCode: 503, statusMessage: 'IKEv2-сервер ещё не развёрнут (запусти стадию 27-ikev2)' })
  }

  const cfg = useRuntimeConfig()
  // Серверный endpoint для IKEv2 — IP entry (по решению TZ §14.1).
  // В config.yaml поля «entry.host» нет в runtime cfg напрямую; берём ENTRY_HOST
  // env (стадия 27 пишет server-cert с CN=$ENTRY_HOST). Иначе — wg public host
  // как fallback (он тоже IP entry).
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

  return await buildIkev2ClientInfo(deviceId, serverIp)
})
