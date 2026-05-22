import { eq } from 'drizzle-orm'
import { useDb } from '../../../database/client'
import { clients, devices } from '../../../database/schema'
import { requireAuth } from '../../../utils/auth'
import { notifyBot } from '../../../utils/bot-events'
import { reissueDeviceWg, syncWireguardConfig } from '../../../utils/wireguard'
import { reissueDeviceOvpn, syncOpenvpnConfig } from '../../../utils/openvpn'

/** Перевыпуск ключей ВСЕХ девайсов клиента (кнопка в шапке модалки). */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })

  const db = useDb()
  const [client] = await db.select({ id: clients.id, tgChatId: clients.tgChatId })
    .from(clients).where(eq(clients.id, id)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  const devRows = await db.select({ id: devices.id }).from(devices).where(eq(devices.clientId, id))
  for (const d of devRows) {
    await reissueDeviceWg(d.id)
    await reissueDeviceOvpn(d.id)
  }

  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after client reissue failed'))
  await syncOpenvpnConfig().catch(err => useLogger().error({ err }, 'ovpn sync after client reissue failed'))

  // Уведомить привязанного клиента — ключи перевыпустил администратор.
  if (client.tgChatId) {
    void notifyBot('client_keys_reissued', { chatId: client.tgChatId })
  }

  return { ok: true, devices: devRows.length }
})
