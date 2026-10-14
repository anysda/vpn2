import { and, desc, eq, inArray, lt } from 'drizzle-orm'
import { useDb } from '../database/client'
import { useLogger } from './logger'
import { events } from '../database/schema'
import type { EventLevel, PanelEvent } from '../../shared/events'
import { parseOutbound } from '../../shared/outbounds'

/**
 * Журнал событий панели: лента на Обзоре. Запись в журнал не должна ломать
 * само действие — ошибка базы только пишется в лог.
 */

const EVENT_RETENTION_MS = 30 * 24 * 3600 * 1000

/** Подпись «кто сделал» в событиях. */
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

export interface EventInput {
  type: string
  level: EventLevel
  title: string
  detail?: string
  object?: string | null
  actor?: string | null
  /** Повтор с тем же ключом не пишется: recordEvent вернёт false. */
  key?: string
  at?: number
}

/** true — запись добавлена; false — ключ уже был или база недоступна. */
export async function recordEvent(input: EventInput): Promise<boolean> {
  try {
    const rows = await useDb().insert(events).values({
      at: input.at ?? Date.now(),
      type: input.type,
      level: input.level,
      title: input.title,
      detail: input.detail ?? '',
      object: input.object ?? null,
      actor: input.actor ?? null,
      key: input.key ?? null,
    }).onConflictDoNothing({ target: events.key }).returning({ id: events.id })
    return rows.length > 0
  }
  catch (err) {
    useLogger().error({ err, type: input.type }, 'event journal write failed')
    return false
  }
}

/** Подпись с исполнителем через «·», пустые части пропускаются. */
export function joinDetail(...parts: Array<string | null | undefined | false>): string {
  return parts.filter(Boolean).join(' · ')
}

export async function recentEvents(limit: number): Promise<PanelEvent[]> {
  const rows = await useDb().select({
    id: events.id, at: events.at, type: events.type, level: events.level, title: events.title, detail: events.detail,
  }).from(events).orderBy(desc(events.at), desc(events.id)).limit(limit)
  return rows
}

/** Последнее событие объекта из перечисленных типов — состояние после рестарта панели. */
export async function lastEventOf(object: string, types: string[]): Promise<{ type: string, at: number } | null> {
  const [row] = await useDb().select({ type: events.type, at: events.at }).from(events)
    .where(and(eq(events.object, object), inArray(events.type, types)))
    .orderBy(desc(events.at), desc(events.id)).limit(1)
  return row ?? null
}

export async function pruneEvents(now = Date.now()): Promise<number> {
  const removed = await useDb().delete(events).where(lt(events.at, now - EVENT_RETENTION_MS)).returning({ id: events.id })
  return removed.length
}
