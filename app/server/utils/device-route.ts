import { and, eq } from 'drizzle-orm'
import type { H3Event } from 'h3'
import { useDb } from '../database/client'
import { clients, devices } from '../database/schema'
import { requireAuth } from './auth'
import { clientByChat, requireBotAuth } from './bot-api'

type Client = typeof clients.$inferSelect
type Device = typeof devices.$inferSelect

/**
 * Проверки маршрута между разбором параметров и поиском в базе (протокол,
 * секрет бота) в его нынешнем порядке. Результат отдаётся как `ready`.
 */
export type Preflight<T> = () => T | Promise<T>

export interface ClientDevice<T> {
  client: Client
  device: Device
  ready: T
}

/** Номер записи из адреса: целое больше нуля, как в удалении и перевыпуске. */
const validId = (id: number) => Number.isSafeInteger(id) && id > 0

async function deviceOf(clientId: number, deviceId: number): Promise<Device> {
  const [device] = await useDb().select().from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, clientId)))
    .limit(1)
  if (!device) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  return device
}

/** Девайс клиента для маршрутов админки `clients/[id]/devices/[deviceId]/*`. */
export async function adminClientDevice<T = undefined>(event: H3Event, preflight?: Preflight<T>): Promise<ClientDevice<T>> {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!validId(clientId) || !validId(deviceId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }
  const ready = await preflight?.() as T
  const [client] = await useDb().select().from(clients).where(eq(clients.id, clientId)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  return { client, device: await deviceOf(clientId, deviceId), ready }
}

/**
 * Девайс клиента, привязанного к чату, для маршрутов бота `bot/devices/[deviceId]/*`.
 * Привязку чата перед ответом маршрут проверяет сам (`requireChatLinked`).
 */
export async function botClientDevice<T = undefined>(event: H3Event, preflight?: Preflight<T>): Promise<ClientDevice<T> & { chatId: number }> {
  requireBotAuth(event)
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  const chatId = Number(getQuery(event).chatId)
  if (!validId(deviceId) || !Number.isSafeInteger(chatId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_params' })
  }
  const ready = await preflight?.() as T
  const client = await clientByChat(chatId)
  return { client, device: await deviceOf(client.id, deviceId), ready, chatId }
}
