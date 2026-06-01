import { timingSafeEqual } from 'node:crypto'
import { asc, eq } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'
import { deviceConfigName } from './naming'

const LOOPBACK_ADDRS = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1'])

function isLoopback(event: H3Event): boolean {
  const ip = event.node.req.socket.remoteAddress ?? ''
  return LOOPBACK_ADDRS.has(ip)
}

/**
 * Bearer-аутентификация бота по TGBOT_SECRET. Дополнительно требует, чтобы
 * запрос пришёл с loopback (бот сидит на той же ноде, что и панель). Это
 * закрывает сценарий, в котором Caddy случайно проксирует /api/bot/** наружу.
 * См. SECURITY-AUDIT-2026-06-01.md (H3, H4).
 */
export function requireBotAuth(event: H3Event): void {
  if (!isLoopback(event)) {
    throw createError({ statusCode: 403, statusMessage: 'loopback_only' })
  }
  const expected = String(useRuntimeConfig().tgbotSecret ?? '')
  if (!expected) throw createError({ statusCode: 503, statusMessage: 'tgbot_secret_not_configured' })
  const provided = (getHeader(event, 'authorization') ?? '').replace(/^Bearer\s+/i, '')
  const a = Buffer.from(provided)
  const b = Buffer.from(expected)
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    throw createError({ statusCode: 401, statusMessage: 'unauthorized' })
  }
}

/** Клиент, привязанный к данному Telegram-чату. 404 `not_linked`, если нет. */
export async function clientByChat(chatId: number) {
  const db = useDb()
  const [c] = await db.select().from(clients).where(eq(clients.tgChatId, chatId)).limit(1)
  if (!c) throw createError({ statusCode: 404, statusMessage: 'not_linked' })
  return c
}

/** Сводка клиента для бота: имя, лимит девайсов и список девайсов. */
export async function botClientView(clientId: number) {
  const db = useDb()
  const [c] = await db.select().from(clients).where(eq(clients.id, clientId)).limit(1)
  if (!c) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  const devRows = await db
    .select()
    .from(devices)
    .where(eq(devices.clientId, clientId))
    .orderBy(asc(devices.createdAt))
  return {
    id: c.id,
    name: c.name,
    deviceLimit: c.deviceLimit,
    expiresAt: c.expiresAt,
    devices: devRows.map(d => ({
      id: d.id,
      name: d.name,
      configName: deviceConfigName(c.name, d.name),
      hasWg: !!(d.wgPrivateKey && d.wgPublicKey && d.wgIp),
      hasOvpn: !!(d.ovpnCert && d.ovpnKey),
    })),
  }
}
