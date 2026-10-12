import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../../database/client'
import { clients, devices } from '../../../../../database/schema'
import { requireAuth } from '../../../../../utils/auth'
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
    throw createError({ statusCode: 503, statusMessage: 'ikev2_not_deployed', message: 'IKEv2-сервер ещё не развёрнут (стадия 27-ikev2)' })
  }

  const cfg = useRuntimeConfig()
  const secret = String(cfg.tgbotSecret ?? '')
  if (!secret) throw createError({ statusCode: 503, statusMessage: 'bot_not_configured', message: 'Telegram-бот не настроен' })
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
  const port = Number(cfg.tgbotEventPort ?? 8877)
  try {
    await $fetch(`http://127.0.0.1:${port}/event`, {
      method: 'POST',
      headers: { 'X-Tgbot-Secret': secret },
      body: {
        type: 'client_send_ikev2',
        clientName: client.name,
        username: info.username,
        server: info.server,
        password: info.password,
        caCertPem: info.caCertPem,
      },
      timeout: 8000,
    })
  }
  catch {
    throw createError({ statusCode: 502, statusMessage: 'bot_unreachable', message: 'Бот не отвечает — проверь /api/admin/telegram' })
  }

  return { ok: true as const }
})
