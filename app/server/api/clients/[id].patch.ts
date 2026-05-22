import { and, count, eq, ne } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { clients, devices } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { notifyClient } from '../../utils/bot-events'
import { minExpiryMs } from '../../utils/expiry'
import { syncWireguardConfig } from '../../utils/wireguard'
import { syncOpenvpnConfig } from '../../utils/openvpn'

const Body = z.object({
  name: z.string().min(1).max(64).optional(),
  expiresAt: z.iso.datetime().nullable().optional(),
  // null — безлимит.
  deviceLimit: z.number().int().min(1).max(999).nullable().optional(),
  frozenManual: z.boolean().optional(),
})

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })

  const body = await readValidatedBody(event, Body.parse)
  const db = useDb()

  // Срок действия нельзя поставить раньше завтрашней даты.
  if (body.expiresAt && new Date(body.expiresAt).getTime() < minExpiryMs()) {
    throw createError({
      statusCode: 422,
      statusMessage: 'Срок действия не может быть раньше завтрашней даты',
    })
  }

  const [before] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!before) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  // Лимит девайсов нельзя опустить ниже текущего числа устройств клиента.
  if (typeof body.deviceLimit === 'number') {
    const [dc] = await db.select({ n: count() }).from(devices).where(eq(devices.clientId, id))
    const have = Number(dc?.n ?? 0)
    if (body.deviceLimit < have) {
      throw createError({
        statusCode: 409,
        statusMessage: `У клиента уже ${have} устройств — лимит не может быть меньше`,
      })
    }
  }

  const patch: Partial<typeof clients.$inferInsert> = { updatedAt: new Date() }
  if (body.name !== undefined) {
    const name = body.name.trim()
    const dup = await db.select({ id: clients.id }).from(clients)
      .where(and(eq(clients.name, name), ne(clients.id, id)))
      .limit(1)
    if (dup.length > 0) {
      throw createError({ statusCode: 409, statusMessage: `Клиент с именем «${name}» уже существует` })
    }
    patch.name = name
  }
  if (body.expiresAt !== undefined) {
    patch.expiresAt = body.expiresAt ? new Date(body.expiresAt) : null
  }
  if (body.deviceLimit !== undefined) patch.deviceLimit = body.deviceLimit
  if (body.frozenManual !== undefined) patch.frozenManual = body.frozenManual

  const [row] = await db.update(clients).set(patch).where(eq(clients.id, id)).returning()
  if (!row) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  // Смена ручной заморозки или срока меняет активность клиента —
  // пере-синхроним девайсы (wg0.conf / CCD).
  if (body.frozenManual !== undefined || body.expiresAt !== undefined) {
    await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after patch failed'))
    await syncOpenvpnConfig().catch(err => useLogger().error({ err }, 'ovpn sync after patch failed'))
  }

  // Уведомления привязанному клиенту об изменениях его аккаунта.
  const chat = before.tgChatId
  if (chat) {
    if (body.deviceLimit !== undefined) {
      // null (безлимит) считаем «выше» любого числа.
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
    if (body.frozenManual !== undefined && body.frozenManual !== before.frozenManual) {
      notifyClient(chat, body.frozenManual
        ? '❄️ Ваш доступ к VPN приостановлен администратором.'
        : '✅ Ваш доступ к VPN восстановлен.')
    }
    if (body.expiresAt !== undefined) {
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
})
