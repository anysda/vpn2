import { and, count, eq, ne } from 'drizzle-orm'
import { useDb } from '../database/client'
import type { Client } from '../database/schema'
import { clients, devices } from '../database/schema'
import { notifyBot, notifyClient } from './bot-events'
import { minExpiryMs } from './expiry'
import { generateClientPassword } from './password'
import { syncWireguardConfig } from './wireguard'
import { caReady, ovpnCn, revokeClientCert, setCcdDisabled, syncOpenvpnConfig } from './openvpn'

/**
 * Общие операции над клиентами — используются и панелью (session-auth),
 * и ботом (Bearer-auth админский чат). Здесь же — валидация и уведомления
 * привязанному клиенту в Telegram.
 */

export interface ClientPatch {
  name?: string
  expiresAt?: string | null
  deviceLimit?: number | null
  frozenManual?: boolean
}

export interface ClientCreateInput {
  name: string
  filterTraffic?: boolean
  expiresAt?: string | null
  deviceLimit?: number | null
}

/** Срок действия нельзя ставить раньше завтрашней даты. */
function assertExpiryAllowed(expiresAt: string | null | undefined): void {
  if (expiresAt && new Date(expiresAt).getTime() < minExpiryMs()) {
    throw createError({
      statusCode: 422,
      statusMessage: 'Срок действия не может быть раньше завтрашней даты',
    })
  }
}

/** Создать клиента (имя + опции). Пароль генерится автоматически. */
export async function createClient(input: ClientCreateInput): Promise<Client> {
  const db = useDb()
  assertExpiryAllowed(input.expiresAt)

  const name = input.name.trim()
  const dup = await db.select({ id: clients.id }).from(clients).where(eq(clients.name, name)).limit(1)
  if (dup.length > 0) {
    throw createError({ statusCode: 409, statusMessage: `Клиент с именем «${name}» уже существует` })
  }

  const values: typeof clients.$inferInsert = {
    name,
    filterTraffic: input.filterTraffic ?? true,
    password: generateClientPassword(),
  }
  if (input.expiresAt !== undefined) values.expiresAt = input.expiresAt ? new Date(input.expiresAt) : null
  if (input.deviceLimit !== undefined) values.deviceLimit = input.deviceLimit

  const [row] = await db.insert(clients).values(values).returning()
  void notifyBot('client_created', { name: row.name })
  return row
}

/** Обновить клиента: валидация, апдейт, ресинк WG/OVPN, уведомления клиенту. */
export async function updateClient(id: number, patch: ClientPatch): Promise<Client> {
  const db = useDb()
  assertExpiryAllowed(patch.expiresAt)

  const [before] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!before) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  // Лимит девайсов нельзя опустить ниже текущего числа устройств клиента.
  if (typeof patch.deviceLimit === 'number') {
    const [dc] = await db.select({ n: count() }).from(devices).where(eq(devices.clientId, id))
    const have = Number(dc?.n ?? 0)
    if (patch.deviceLimit < have) {
      throw createError({
        statusCode: 409,
        statusMessage: `У клиента уже ${have} устройств — лимит не может быть меньше`,
      })
    }
  }

  const upd: Partial<typeof clients.$inferInsert> = { updatedAt: new Date() }
  if (patch.name !== undefined) {
    const name = patch.name.trim()
    const dup = await db.select({ id: clients.id }).from(clients)
      .where(and(eq(clients.name, name), ne(clients.id, id)))
      .limit(1)
    if (dup.length > 0) {
      throw createError({ statusCode: 409, statusMessage: `Клиент с именем «${name}» уже существует` })
    }
    upd.name = name
  }
  if (patch.expiresAt !== undefined) upd.expiresAt = patch.expiresAt ? new Date(patch.expiresAt) : null
  if (patch.deviceLimit !== undefined) upd.deviceLimit = patch.deviceLimit
  if (patch.frozenManual !== undefined) upd.frozenManual = patch.frozenManual

  const [row] = await db.update(clients).set(upd).where(eq(clients.id, id)).returning()
  if (!row) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  // Смена заморозки/срока меняет активность — пере-синхроним девайсы.
  if (patch.frozenManual !== undefined || patch.expiresAt !== undefined) {
    await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after client update failed'))
    await syncOpenvpnConfig().catch(err => useLogger().error({ err }, 'ovpn sync after client update failed'))
  }

  // Уведомления привязанному клиенту об изменениях аккаунта.
  const chat = before.tgChatId
  if (chat) {
    if (patch.name !== undefined && row.name !== before.name) {
      notifyClient(chat, `✏️ Имя вашего профиля изменено на «${row.name}».`)
    }
    if (patch.deviceLimit !== undefined) {
      const rank = (v: number | null) => (v === null ? Infinity : v)
      const oldR = rank(before.deviceLimit)
      const newR = rank(row.deviceLimit)
      if (newR > oldR) {
        notifyClient(chat, row.deviceLimit === null
          ? '📈 Лимит устройств снят — теперь без ограничений.'
          : `📈 Ваш лимит устройств повышен до ${row.deviceLimit}.`)
      }
      else if (newR < oldR) {
        notifyClient(chat, `📉 Ваш лимит устройств понижен до ${row.deviceLimit}.`)
      }
    }
    if (patch.frozenManual !== undefined && patch.frozenManual !== before.frozenManual) {
      notifyClient(chat, patch.frozenManual
        ? '❄️ Ваш доступ к VPN приостановлен администратором.'
        : '✅ Ваш доступ к VPN восстановлен.')
    }
    if (patch.expiresAt !== undefined) {
      const oldT = before.expiresAt ? before.expiresAt.getTime() : null
      const newT = row.expiresAt ? row.expiresAt.getTime() : null
      if (oldT !== newT) {
        notifyClient(chat, row.expiresAt
          ? `🗓 Срок действия вашего ключа: до ${row.expiresAt.toLocaleDateString('ru-RU')}.`
          : '🗓 Срок действия ключа снят — доступ бессрочный.')
      }
    }
  }

  return row
}

/** Удалить клиента: каскадно девайсы, ресинк WG, отзыв OVPN-сертификатов. */
export async function deleteClient(id: number): Promise<void> {
  const db = useDb()
  const devRows = await db.select().from(devices).where(eq(devices.clientId, id))

  const result = await db.delete(clients).where(eq(clients.id, id)).returning()
  if (result.length === 0) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after client delete failed'))

  if (devRows.length && await caReady()) {
    for (const d of devRows) {
      if (!d.ovpnCert) continue
      await revokeClientCert(d.ovpnCert).catch(err =>
        useLogger().error({ err }, 'ovpn revoke after client delete failed'))
      await setCcdDisabled(ovpnCn(d.id), false).catch(() => {})
    }
  }
}
