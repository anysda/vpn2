import { Buffer } from 'node:buffer'
import { randomUUID } from 'node:crypto'
import { and, asc, count, desc, eq, gt, inArray, lt, lte, ne, or } from 'drizzle-orm'
import type { BotDelivery } from '../../shared/bot-delivery'
import type { EventLevel } from '../../shared/events'
import { parseOutbound } from '../../shared/outbounds'
import { useDb } from '../database/client'
import { botOutbox, events } from '../database/schema'

// Журнал событий панели (лента Обзора) и очередь уведомлений боту этой ноды.
const EVENT_RETENTION_MS = 30 * 24 * 3600 * 1000

export const BOT_ACTOR = 'через бота'

export function actorOf(user: { username: string }): string {
  return user.username ? `@${user.username}` : 'панель'
}

/** Выход маршрута так, как его подписывает панель: «DE», «DE WARP». */
export function outboundLabel(outbound: string): string {
  const parsed = parseOutbound(outbound)
  if (!parsed) return outbound
  return `${parsed.tag.toUpperCase()}${parsed.variant === 'warp' ? ' WARP' : ''}`
}

/**
 * true - запись добавлена; false - ключ `key` уже был или база недоступна: сбой журнала не
 * ломает само действие.
 */
export async function recordEvent(input: {
  type: string, level: EventLevel, title: string, detail?: string, object?: string | null, actor?: string | null, key?: string, at?: number
}): Promise<boolean> {
  try {
    const rows = await useDb().insert(events).values({ ...input, at: input.at ?? Date.now(), detail: input.detail ?? '' })
      .onConflictDoNothing({ target: events.key }).returning({ id: events.id })
    return rows.length > 0
  }
  catch (err) {
    useLogger().error({ err, type: input.type }, 'event journal write failed')
    return false
  }
}

export function joinDetail(...parts: Array<string | null | undefined | false>): string {
  return parts.filter(Boolean).join(' · ')
}

export const recentEvents = (limit: number) => useDb().select({
  id: events.id, at: events.at, type: events.type, level: events.level, title: events.title, detail: events.detail,
}).from(events).orderBy(desc(events.at), desc(events.id)).limit(limit)

/** Последнее событие объекта из перечисленных типов - состояние после рестарта панели. */
export async function lastEventOf(object: string, types: string[]): Promise<{ type: string, at: number, detail: string } | null> {
  const [row] = await useDb().select({ type: events.type, at: events.at, detail: events.detail }).from(events)
    .where(and(eq(events.object, object), inArray(events.type, types)))
    .orderBy(desc(events.at), desc(events.id)).limit(1)
  return row ?? null
}

export const pruneEvents = () => useDb().delete(events).where(lt(events.at, Date.now() - EVENT_RETENTION_MS))

const TELEGRAM_PATH = '/var/lib/anysda-vpn2/runtime/telegram-runtime.json'

/** admin_username - ник администратора бота без @: бот называет его людям без доступа. */
export function readTelegramRuntime() {
  const parsed = readJsonFile<Record<string, unknown>>(TELEGRAM_PATH) ?? {}
  return {
    bot_token: String(parsed.bot_token ?? ''),
    chat_id: (parsed.chat_id ?? '') as string | number,
    admin_username: String(parsed.admin_username ?? ''),
  }
}

export const writeTelegramRuntime = (data: ReturnType<typeof readTelegramRuntime>) => writeJsonFile(TELEGRAM_PATH, data)

/** Настроен = есть кому доставлять: секрет бота и токен с чатом из настроек, условие значка «НЕНАСТРОЕН». */
export function botConfigured(): boolean {
  const runtime = readTelegramRuntime()
  return !!useRuntimeConfig().tgbotSecret && !!runtime.bot_token && !!runtime.chat_id
}

export function maskToken(token: string): string {
  if (!token) return ''
  if (token.length < 10) return '••••••'
  return `${token.slice(0, 6)}..${token.slice(-4)}`
}

/**
 * Жив = /health бота ответил 200 без waiting_token: с отвергнутым токеном порт бота остаётся в
 * LISTEN без обработчика. Имя бот узнаёт у Telegram (getMe) и отдаёт в том же ответе.
 */
export async function botHealth(): Promise<{ running: boolean, username: string }> {
  const body = await fetch(botUrl('health'), { signal: AbortSignal.timeout(1000) })
    .then(res => res.ok ? res.json().catch(() => ({})) : null, () => null) as { username?: unknown, waiting_token?: unknown } | null
  if (!body || body.waiting_token === true) return { running: false, username: '' }
  return { running: true, username: typeof body.username === 'string' ? body.username : '' }
}

export function botSecretOrThrow() {
  if (!useRuntimeConfig().tgbotSecret) throw createError({ statusCode: 503, statusMessage: 'bot_not_configured', message: 'Telegram-бот не настроен' })
}

const botUrl = (path: string) => `http://127.0.0.1:${Number(useRuntimeConfig().tgbotEventPort ?? 8877)}/${path}`

function postBotEvent(body: Record<string, unknown>, timeout: number): Promise<unknown> {
  return $fetch<unknown>(botUrl('event'), {
    method: 'POST', headers: { 'X-Tgbot-Secret': String(useRuntimeConfig().tgbotSecret) }, body, timeout, retry: 0,
  })
}

export async function sendBotEvent(body: Record<string, unknown>): Promise<void> {
  await postBotEvent(body, 8000).catch((cause) => {
    throw createError({ statusCode: 502, statusMessage: 'bot_unreachable', message: 'Бот не отвечает — проверь /api/admin/telegram', cause })
  })
}

const DELAYS = [30, 60, 120, 240, 480].map(seconds => seconds * 1000)
const MAX_ATTEMPTS = DELAYS.length + 1
const TTL = 60 * 60 * 1000
const RETENTION = 24 * TTL
const CAPACITY = 1024
const inFlight = new Map<string, Promise<void>>()

function delivery(row: Pick<typeof botOutbox.$inferSelect, 'id' | 'label' | 'status' | 'attempts' | 'created'>): BotDelivery {
  return { id: row.id, label: row.label, status: row.status, remaining: row.status === 'queued' ? MAX_ATTEMPTS - row.attempts : 0, created: row.created }
}

export async function notifyBot(type: string, payload: Record<string, unknown>, label = `Уведомление о клиенте «${payload.name ?? '?'}»`, single = false): Promise<BotDelivery | null> {
  if (!botConfigured()) return null
  const now = Date.now()
  const body = JSON.stringify({ ...payload, type })
  if (Buffer.byteLength(body) > 128 * 1024) throw createError({ statusCode: 413, message: 'Уведомление слишком большое. Повторов не будет.' })
  const row = { id: randomUUID(), label, payload: body, status: 'queued' as const, attempts: 0, due: now, expires: now + TTL, created: now }
  const pending = await useDb().transaction(async (tx) => {
    if (single) {
      const [waiting] = await tx.select().from(botOutbox).where(and(eq(botOutbox.label, label), eq(botOutbox.status, 'queued'))).limit(1)
      if (waiting) return waiting
    }
    await tx.delete(botOutbox).where(lt(botOutbox.created, now - RETENTION))
    const [size] = await tx.select({ n: count() }).from(botOutbox)
    if (Number(size?.n) >= CAPACITY) {
      const [old] = await tx.select({ id: botOutbox.id }).from(botOutbox).where(eq(botOutbox.status, 'accepted')).orderBy(asc(botOutbox.created)).limit(1)
      if (!old) throw createError({ statusCode: 503, message: `${label}: очередь заполнена. Повторов не будет.` })
      await tx.delete(botOutbox).where(eq(botOutbox.id, old.id))
    }
    await tx.insert(botOutbox).values(row)
  })
  if (pending) return delivery(pending)
  await attempt(row.id)
  const [saved] = await useDb().select().from(botOutbox).where(eq(botOutbox.id, row.id))
  return delivery(saved ?? { ...row, status: 'accepted' })
}

async function deliveryFailed(label: string): Promise<void> {
  await recordEvent({ type: 'bot_delivery_failed', level: 'alarm', object: 'bot', title: 'Бот не принял уведомление', detail: label })
}

/** Одна попытка на запись за раз: повтор из таймера ждёт идущую. */
function attempt(id: string): Promise<void> {
  if (!inFlight.has(id)) inFlight.set(id, attemptOnce(id).finally(() => inFlight.delete(id)))
  return inFlight.get(id)!
}

async function attemptOnce(id: string) {
  const db = useDb()
  const now = Date.now()
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
    if (!useRuntimeConfig().tgbotSecret) throw new Error('bot not configured')
    const ack = await postBotEvent({ ...JSON.parse(row.payload), deliveryId: id }, 1500) as { ok: boolean, delivery_id: string }
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

export async function notifyBackupFailed(detail: string): Promise<void> {
  await notifyWarning(notifyBot('system_alert', { subject: 'Бэкап не удался', detail }, 'Сообщение об отказе бэкапа', true))
}

export function notifyWarning(send: Promise<unknown>, fallback = 'Уведомление клиенту не отправлено'): Promise<string | undefined> {
  return send.then(() => undefined, (err: unknown) => {
    useLogger().warn({ err }, 'notification not queued')
    return (err as { statusCode?: number }).statusCode && err instanceof Error ? err.message : fallback
  })
}
