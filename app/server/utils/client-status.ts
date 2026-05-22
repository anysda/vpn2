import type { Client } from '../database/schema'

/** Поля клиента, достаточные для вычисления статуса. */
export type ClientStatusInput = Pick<Client, 'frozenManual' | 'expiresAt'>

/**
 * Клиент активен, если НЕ заморожен вручную И срок действия не истёк.
 * Статус нигде не хранится — всегда вычисляется отсюда. По истечении срока
 * клиент не удаляется, а становится «заморожен»; продление срока возвращает
 * его в «активен» (если не заморожен вручную).
 */
export function isClientActive(client: ClientStatusInput): boolean {
  if (client.frozenManual) return false
  if (client.expiresAt && client.expiresAt.getTime() <= Date.now()) return false
  return true
}

/** Статус клиента для отдачи во фронтенд / бот. */
export function clientStatus(client: ClientStatusInput): 'active' | 'frozen' {
  return isClientActive(client) ? 'active' : 'frozen'
}
