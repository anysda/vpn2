import { and, eq, ne } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { notifyBot } from '../../utils/bot-events'
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

  const [before] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!before) throw createError({ statusCode: 404, statusMessage: 'not_found' })

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

  // Повышение лимита девайсов → уведомить привязанного клиента в боте.
  // Понижение/прочие правки клиенту не шлём — клиентам алертов не отправляем.
  if (body.deviceLimit !== undefined && before.tgChatId) {
    const oldL = before.deviceLimit
    const newL = row.deviceLimit
    const raised = (newL === null && oldL !== null)
      || (typeof newL === 'number' && typeof oldL === 'number' && newL > oldL)
    if (raised) {
      void notifyBot('client_quota_raised', { chatId: before.tgChatId, deviceLimit: newL })
    }
  }

  return row
})
