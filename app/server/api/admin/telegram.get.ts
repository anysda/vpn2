import { and, count, eq, gte, isNotNull } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { botOutbox, clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { botHealth, maskToken, readTelegramRuntime } from '../../utils/telegram-config'

const DAY = 24 * 60 * 60 * 1000

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const cfg = readTelegramRuntime()
  const db = useDb()
  const [health, [all], [subscribed], [queued], [delivered]] = await Promise.all([
    botHealth(),
    db.select({ n: count() }).from(clients),
    db.select({ n: count() }).from(clients).where(isNotNull(clients.tgChatId)),
    db.select({ n: count() }).from(botOutbox).where(eq(botOutbox.status, 'queued')),
    // Принятое ботом за сутки; очередь панели хранит записи те же 24 часа.
    db.select({ n: count() }).from(botOutbox).where(and(eq(botOutbox.status, 'accepted'), gte(botOutbox.created, Date.now() - DAY))),
  ])
  return {
    configured: !!cfg.bot_token && !!cfg.chat_id,
    running: health.running,
    username: health.username,
    bot_token_masked: maskToken(cfg.bot_token),
    chat_id: cfg.chat_id,
    admin_username: cfg.admin_username,
    clients_total: all?.n ?? 0,
    clients_subscribed: subscribed?.n ?? 0,
    queued: queued?.n ?? 0,
    delivered_day: delivered?.n ?? 0,
  }
})
