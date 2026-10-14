import { and, eq, gt, lte } from 'drizzle-orm'
import { pluralRu } from '../../shared/plural'
import { useDb } from '../database/client'
import { clients } from '../database/schema'
import { notifyClient } from './bot-events'
import { joinDetail, recordEvent } from './events'
import { useLogger } from './logger'
import { botConfigured } from './telegram-config'

const DAY_MS = 86_400_000
const EXPIRY_NOTICE_MS = 3 * DAY_MS

/**
 * За 3 дня до конца срока — запись в журнал и одно напоминание клиенту в
 * Telegram. Ключ записи включает сам срок: напоминание уходит один раз на
 * срок и не повторяется после перезапуска панели; продлили срок — новый ключ.
 */
export async function remindExpiring(now = Date.now()): Promise<void> {
  const soon = await useDb().select({ id: clients.id, name: clients.name, expiresAt: clients.expiresAt, tgChatId: clients.tgChatId })
    .from(clients)
    .where(and(gt(clients.expiresAt, new Date(now)), lte(clients.expiresAt, new Date(now + EXPIRY_NOTICE_MS)), eq(clients.frozenManual, false)))
  for (const c of soon) {
    const ends = c.expiresAt!
    const days = Math.max(1, Math.ceil((ends.getTime() - now) / DAY_MS))
    const left = `через ${days} ${pluralRu(days, ['день', 'дня', 'дней'])}`
    const telegram = Boolean(c.tgChatId) && botConfigured()
    const fresh = await recordEvent({
      type: 'client_expiring', level: 'warn', object: `client:${c.id}`, at: now, key: `expiring:${c.id}:${ends.getTime()}`,
      title: `Доступ истекает · ${c.name}`, detail: joinDetail(left, telegram && 'напоминание ушло в Telegram'),
    })
    if (!fresh || !telegram) continue
    const date = ends.toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' })
    // Сбой постановки в очередь бота виден в журнале отдельной тревогой.
    await notifyClient(c.tgChatId!, `⏳ Доступ к VPN закончится ${left} — ${date}. Чтобы продлить, напишите администратору.`, c.name)
      .catch(err => useLogger().warn({ err, client: c.id }, 'expiry reminder not queued'))
  }
}
