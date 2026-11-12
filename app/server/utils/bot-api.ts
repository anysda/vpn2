import { timingSafeEqual } from 'node:crypto'
import { and, eq } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'
import { accessStatus } from '../../shared/access-operation'

/**
 * Bearer бота по TGBOT_SECRET, только напрямую с этой ноды: Caddy тоже ходит с 127.0.0.1,
 * но ставит X-Forwarded-For. Основной замок - 404 на эти пути в Caddyfile.
 */
export function requireBotAuth(event: H3Event): void {
  const h = event.node.req.headers
  if (!isLoopbackAddr(event.node.req.socket.remoteAddress) || h['x-forwarded-for'] || h['x-forwarded-host'] || h.forwarded || h.via) {
    throw createError({ statusCode: 403, statusMessage: 'loopback_only' })
  }
  const expected = Buffer.from(String(useRuntimeConfig().tgbotSecret ?? ''))
  if (!expected.length) throw createError({ statusCode: 503, statusMessage: 'tgbot_secret_not_configured' })
  const provided = Buffer.from((getHeader(event, 'authorization') ?? '').replace(/^Bearer\s+/i, ''))
  if (provided.length !== expected.length || !timingSafeEqual(provided, expected)) {
    throw createError({ statusCode: 401, statusMessage: 'unauthorized' })
  }
}

/** Клиент, привязанный к Telegram-чату. 404 `not_linked`, если такого нет. */
export async function clientByChat(chatId: number) {
  const [c] = await useDb().select().from(clients).where(eq(clients.tgChatId, chatId)).limit(1)
  if (!c) throw createError({ statusCode: 404, statusMessage: 'not_linked' })
  return c
}

/**
 * Чат всё ещё привязан к клиенту. Ответ с секретом (конфиг) перепроверяет это
 * последним await: администратор мог отвязать Telegram, пока шёл запрос.
 */
export async function requireChatLinked(clientId: number, chatId: number): Promise<void> {
  const [linked] = await useDb().select({ id: clients.id }).from(clients)
    .where(and(eq(clients.id, clientId), eq(clients.tgChatId, chatId))).limit(1)
  if (!linked) throw createError({ statusCode: 404, statusMessage: 'not_linked' })
}

/** Сводка клиента для бота: имя, лимит и список устройств. */
export async function botClientView(clientId: number) {
  const db = useDb()
  const [c] = await db.select().from(clients).where(eq(clients.id, clientId)).limit(1)
  if (!c) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  const devRows = await db.select().from(devices).where(eq(devices.clientId, clientId)).orderBy(...devicesOldestFirst)
  const operation = await latestAccess(c.id)
  return {
    operation,
    accessStatus: operation ? accessStatus(operation) : 'Состояние не подтверждено',
    id: c.id,
    name: c.name,
    deviceLimit: c.deviceLimit,
    expiresAt: c.expiresAt,
    devices: devRows.map(d => deviceView(c, d)),
  }
}
