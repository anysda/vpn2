import { randomUUID } from 'node:crypto'
import { Buffer } from 'node:buffer'
import { and, asc, count, eq, gt, lt, lte, ne, or } from 'drizzle-orm'
import { useDb } from '../database/client'
import { botOutbox } from '../database/schema'
import type { BotDelivery } from '../../shared/bot-delivery'
import { botConfigured } from './telegram-config'
import { recordEvent } from './events'

const DELAYS = [30, 60, 120, 240, 480].map(seconds => seconds * 1000)
const MAX_ATTEMPTS = DELAYS.length + 1
const TTL = 60 * 60 * 1000
const RETENTION = 24 * TTL
const CAPACITY = 1024
const inFlight = new Map<string, Promise<void>>()

function delivery(row: Pick<typeof botOutbox.$inferSelect, 'id' | 'label' | 'status' | 'attempts' | 'created'>): BotDelivery {
  return { id: row.id, label: row.label, status: row.status, remaining: row.status === 'queued' ? MAX_ATTEMPTS - row.attempts : 0, created: row.created }
}

/**
 * Запись завершена до первого POST. Один UUID на все попытки, в том числе после рестарта.
 * Без настроенного бота доставлять некому: в очередь не ставим, ответ null.
 */
export async function notifyBot(type: string, payload: Record<string, unknown>, label = `Уведомление о клиенте «${payload.name ?? '?'}»`): Promise<BotDelivery | null> {
  if (!botConfigured()) return null
  const now = Date.now()
  const body = JSON.stringify({ ...payload, type })
  if (Buffer.byteLength(body) > 128 * 1024) throw createError({ statusCode: 413, message: 'Уведомление слишком большое. Повторов не будет.' })
  const row = { id: randomUUID(), label, payload: body, status: 'queued' as const, attempts: 0, due: now, expires: now + TTL, created: now }
  await useDb().transaction(async (tx) => {
    await tx.delete(botOutbox).where(lt(botOutbox.created, now - RETENTION))
    // Лимит включает историю. При нехватке места освобождаем только
    // подтверждённую запись; ожидающие и ошибки молча не вытесняем.
    const [size] = await tx.select({ n: count() }).from(botOutbox)
    if (Number(size?.n) >= CAPACITY) {
      const [old] = await tx.select({ id: botOutbox.id }).from(botOutbox).where(eq(botOutbox.status, 'accepted')).orderBy(asc(botOutbox.created)).limit(1)
      if (!old) throw createError({ statusCode: 503, message: `${label}: очередь заполнена. Повторов не будет.` })
      await tx.delete(botOutbox).where(eq(botOutbox.id, old.id))
    }
    await tx.insert(botOutbox).values(row)
  })
  await attempt(row.id)
  const [saved] = await useDb().select().from(botOutbox).where(eq(botOutbox.id, row.id))
  // Конкурентная вставка вправе вытеснить только уже подтверждённую запись.
  return delivery(saved ?? { ...row, status: 'accepted' })
}

/** Повторов больше не будет — тревога в журнал. */
async function deliveryFailed(label: string): Promise<void> {
  await recordEvent({ type: 'bot_delivery_failed', level: 'alarm', object: 'bot', title: 'Бот не принял уведомление', detail: label })
}

function attempt(id: string): Promise<void> {
  const pending = inFlight.get(id)
  if (pending) return pending
  // Если таймер успел занять новую запись, запрос ждёт его результата,
  // а не выдаёт «не отправлено» посреди ещё выполняющегося POST.
  const pendingAttempt = attemptOnce(id).finally(() => { inFlight.delete(id) })
  inFlight.set(id, pendingAttempt)
  return pendingAttempt
}

async function attemptOnce(id: string) {
  const db = useDb()
  const now = Date.now()
  // Атомарная аренда и расход попытки ДО HTTP; два тика/запроса не шлют одновременно.
  const row = await db.transaction(async (tx) => {
    const [candidate] = await tx.select().from(botOutbox).where(and(eq(botOutbox.id, id), eq(botOutbox.status, 'queued'), lte(botOutbox.due, now)))
    if (!candidate) return
    if (candidate.expires <= now || candidate.attempts >= MAX_ATTEMPTS) {
      await tx.update(botOutbox).set({ status: 'failed', payload: '' }).where(eq(botOutbox.id, id))
      return { ...candidate, failed: true }
    }
    const attempts = candidate.attempts + 1
    await tx.update(botOutbox).set({ attempts, due: now + (DELAYS[attempts - 1] ?? 30_000) }).where(eq(botOutbox.id, id))
    return { ...candidate, attempts, failed: false }
  })
  if (!row) return
  if (row.failed) return deliveryFailed(row.label)
  let accepted = false
  try {
    const cfg = useRuntimeConfig()
    if (!cfg.tgbotSecret) throw new Error('bot not configured')
    const ack = await $fetch<{ ok: boolean, delivery_id: string }>(`http://127.0.0.1:${Number(cfg.tgbotEventPort ?? 8877)}/event`, {
      method: 'POST', headers: { 'X-Tgbot-Secret': String(cfg.tgbotSecret) },
      body: { ...JSON.parse(row.payload), deliveryId: id }, timeout: 1500, retry: 0,
    })
    accepted = ack.ok === true && ack.delivery_id === id
  }
  catch { /* Очередь и оставшиеся попытки видны администратору. */ }
  const status = accepted ? 'accepted' : row.attempts >= MAX_ATTEMPTS ? 'failed' : 'queued'
  await db.update(botOutbox).set({
    status, due: Date.now() + (DELAYS[row.attempts - 1] ?? 0),
    ...(!accepted ? { lastFailure: Date.now() } : {}),
    ...(status === 'queued' ? {} : { payload: '' }),
  }).where(eq(botOutbox.id, id))
  if (status === 'failed') await deliveryFailed(row.label)
}

export async function retryBotEvents() {
  const db = useDb()
  const now = Date.now()
  // Ожидающие от времени, когда бот не был настроен (или его сняли), не
  // доставить: удаляем, а не повторяем и не показываем очередью.
  if (!botConfigured()) await db.delete(botOutbox).where(eq(botOutbox.status, 'queued'))
  const expired = await db.update(botOutbox).set({ status: 'failed', payload: '' })
    .where(and(eq(botOutbox.status, 'queued'), lte(botOutbox.expires, now))).returning({ label: botOutbox.label })
  for (const row of expired) await deliveryFailed(row.label)
  await db.delete(botOutbox).where(lt(botOutbox.created, now - RETENTION))
  const rows = await db.select({ id: botOutbox.id }).from(botOutbox)
    .where(and(eq(botOutbox.status, 'queued'), lte(botOutbox.due, now))).orderBy(asc(botOutbox.created)).limit(16)
  for (const row of rows) await attempt(row.id)
}

export async function botDeliveries(): Promise<BotDelivery[]> {
  const shown = botConfigured() ? or(ne(botOutbox.status, 'queued'), gt(botOutbox.lastFailure, 0)) : ne(botOutbox.status, 'queued')
  const rows = await useDb().select({ id: botOutbox.id, label: botOutbox.label, status: botOutbox.status, attempts: botOutbox.attempts, created: botOutbox.created })
    .from(botOutbox).where(shown).orderBy(asc(botOutbox.created))
  return rows.map(delivery)
}

export function notifyClient(chatId: number, text: string, name: string): Promise<BotDelivery | null> {
  return notifyBot('client_notify', { chatId, text }, `Уведомление клиенту «${name}»`)
}

/**
 * Изменение уже записано, уведомление о нём - вдогонку: сбой постановки в
 * очередь возвращается текстом предупреждения для ответа, а не ошибкой запроса.
 * У ошибок очереди текст для человека, прочие только в журнал.
 */
export function notifyWarning(send: Promise<unknown>, fallback = 'Уведомление клиенту не отправлено'): Promise<string | undefined> {
  return send.then(() => undefined, (err: unknown) => {
    useLogger().warn({ err }, 'notification not queued')
    return (err as { statusCode?: number }).statusCode && err instanceof Error ? err.message : fallback
  })
}
