import { timingSafeEqual } from 'node:crypto'
import { and, asc, eq } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'
import { deviceConfigName } from './naming'
import { latestAccess } from './access-operations'
import { accessStatus } from '../../shared/access-operation'

const LOOPBACK_ADDRS = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1'])

/** Адрес сокета — loopback этой ноды. */
export function isLoopbackAddr(ip: string | undefined): boolean {
  return !!ip && LOOPBACK_ADDRS.has(ip)
}

/**
 * Запрос пришёл напрямую с этой ноды, а не снаружи через Caddy.
 *
 * Одного адреса сокета мало: Caddy сам ходит в панель с 127.0.0.1, и любой
 * запрос из интернета выглядит здесь как loopback (VPN2-51). Отличие —
 * X-Forwarded-For: Caddy ставит его на каждый проксируемый запрос, а бот и
 * прочие локальные вызовы идут на 127.0.0.1:51821 мимо него и такого
 * заголовка не шлют. Основной замок — 404 на эти пути в Caddyfile
 * (infra/scripts/30-frontend.sh), это второй.
 */
function isDirectLocal(event: H3Event): boolean {
  if (!isLoopbackAddr(event.node.req.socket.remoteAddress)) return false
  const h = event.node.req.headers
  return !h['x-forwarded-for'] && !h['x-forwarded-host'] && !h.forwarded && !h.via
}

/**
 * Bearer-аутентификация бота по TGBOT_SECRET. Дополнительно требует, чтобы
 * запрос пришёл напрямую с этой ноды (бот сидит на той же ноде, что и панель,
 * и ходит на 127.0.0.1:51821).
 */
export function requireBotAuth(event: H3Event): void {
  if (!isDirectLocal(event)) {
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

/**
 * Чат всё ещё привязан к клиенту. Клиента по чату ищут в начале запроса, а
 * между этим и ответом идут await: администратор мог отвязать Telegram.
 * Ответ с секретом (конфиг) перепроверяет привязку последним await перед
 * возвратом - отвязка, записанная до ответа, его не пропускает.
 */
export async function requireChatLinked(clientId: number, chatId: number): Promise<void> {
  const [linked] = await useDb().select({ id: clients.id }).from(clients)
    .where(and(eq(clients.id, clientId), eq(clients.tgChatId, chatId))).limit(1)
  if (!linked) throw createError({ statusCode: 404, statusMessage: 'not_linked' })
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
  const operation = await latestAccess(c.id)
  return {
    operation,
    accessStatus: operation ? accessStatus(operation) : 'Состояние не подтверждено',
    id: c.id,
    name: c.name,
    deviceLimit: c.deviceLimit,
    expiresAt: c.expiresAt,
    devices: devRows.map(d => ({
      id: d.id,
      name: d.name,
      configName: deviceConfigName(c.name, d.name, d.id),
      hasWg: !!(d.wgPrivateKey && d.wgPublicKey && d.wgIp),
      hasOvpn: !!(d.ovpnCert && d.ovpnKey),
    })),
  }
}
