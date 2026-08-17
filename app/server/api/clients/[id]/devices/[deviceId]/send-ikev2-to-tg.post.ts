import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../../database/client'
import { clients, devices } from '../../../../../database/schema'
import { requireAuth } from '../../../../../utils/auth'
import { notifyBot } from '../../../../../utils/bot-events'
import { buildIkev2ClientInfo, ikev2ServerReady } from '../../../../../utils/ikev2'

/**
 * Послать IKEv2-доступ в Telegram (админский чат). Два сообщения:
 *   1) текст с server/login/password (caption '<username> — IKEv2')
 *   2) ca.crt файлом (caption '<username> — CA-сертификат IKEv2')
 * Платформы не различаем — у нативного IKEv2 на всех ОС одинаковый набор
 * полей и одинаковый CA. Сами .mobileconfig/.sswan не делаем.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!Number.isFinite(clientId) || !Number.isFinite(deviceId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  if (!(await ikev2ServerReady())) {
    throw createError({ statusCode: 503, statusMessage: 'IKEv2-сервер ещё не развёрнут (стадия 27-ikev2)' })
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

  const info = await buildIkev2ClientInfo(deviceId, serverIp)
  await notifyBot('client_send_ikev2', {
    clientName: client.name,
    username: info.username,
    server: info.server,
    password: info.password,
    caCertPem: info.caCertPem,
  }).catch((err) => {
    useLogger().error({ err: (err as Error).message }, 'send-ikev2-to-tg bot event failed')
    throw createError({ statusCode: 502, statusMessage: 'Ошибка передачи в бота' })
  })

  return { ok: true as const }
})
