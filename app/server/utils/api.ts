import type { H3Event } from 'h3'
import { requireAuth } from './auth'
import { requireBotAuth } from './bot-api'

/**
 * Обёртки таблиц маршрутов (`server/resources`). Ключ таблицы - `МЕТОД /путь`
 * под `/api`, регистрирует их `plugins/api.ts`.
 */
type AdminUser = Awaited<ReturnType<typeof requireAuth>>

/** Маршрут панели: только с сессией администратора. */
export const admin = <T>(handler: (event: H3Event, user: AdminUser) => T) =>
  async (event: H3Event) => handler(event, await requireAuth(event))

/** Маршрут бота: секрет бота и запрос с этой же ноды. */
export const bot = <T>(handler: (event: H3Event) => T) => async (event: H3Event) => {
  requireBotAuth(event)
  return handler(event)
}

/** Номер записи: целое больше нуля. */
export const validId = (id: number) => Number.isSafeInteger(id) && id > 0

/** Число из адреса; не число - 400 `invalid_id`. */
export function idParam(event: H3Event, name = 'id'): number {
  const id = Number(getRouterParam(event, name))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  return id
}

/** Номера записей из адреса для операций доступа: каждый - целое больше нуля. */
export function recordIds<N extends string>(event: H3Event, ...names: N[]): Record<N, number> {
  const ids = names.map(name => Number(getRouterParam(event, name)))
  if (!ids.every(validId)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  return Object.fromEntries(names.map((name, i) => [name, ids[i]])) as Record<N, number>
}

/** Ответ «принято в очередь»: 202. */
export function accepted<T>(event: H3Event, result: T): T {
  setResponseStatus(event, 202)
  return result
}

export const notFound = () => createError({ statusCode: 404, statusMessage: 'not_found' })
