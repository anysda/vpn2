import { z } from 'zod'
import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../../database/client'
import { clients, devices } from '../../../../../database/schema'
import { requireAuth } from '../../../../../utils/auth'
import { notifyBot } from '../../../../../utils/bot-events'
import { buildIkev2ClientInfo, ikev2CaReady, renderClientMobileconfig } from '../../../../../utils/ikev2'

const Body = z.object({ platform: z.enum(['ios', 'android', 'windows']) })

/**
 * Послать клиенту IKEv2 в Telegram:
 *   - ios     → 1 сообщение: .mobileconfig файлом
 *   - android → 4 сообщения: server / username / password / ca.crt
 *   - windows → 4 сообщения: server / username / password / ca.crt
 *
 * Сам бот рендерит сообщения; панель шлёт payload через /event с одним
 * типом и payload'ом, бот разруливает по platform.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!Number.isFinite(clientId) || !Number.isFinite(deviceId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }
  const body = await readValidatedBody(event, Body.parse)

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

  const info = await buildIkev2ClientInfo(deviceId, serverIp)
  // Шлём в АДМИНСКИЙ чат (как WG/OVPN) — админ пересылает клиенту.
  // Имя в caption — «<клиент> · <устройство>» для понятности при пересылке.
  const name = `${client.name} · ${device.name}`
  const payload: Record<string, unknown> = {
    name,
    platform: body.platform,
    server: info.server,
    username: info.username,
    password: info.password,
    caCertPem: info.caCertPem,
  }
  if (body.platform === 'ios') {
    payload.mobileconfig = await renderClientMobileconfig(deviceId, serverIp)
    payload.fileName = `anysda-ikev2-${info.username}.mobileconfig`
  }

  await notifyBot('client_send_ikev2', payload).catch((err) => {
    useLogger().error({ err: (err as Error).message }, 'send-ikev2-to-tg bot event failed')
    throw createError({ statusCode: 502, statusMessage: 'Ошибка передачи в бота' })
  })

  return { ok: true as const }
})
