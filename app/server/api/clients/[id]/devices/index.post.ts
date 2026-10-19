import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../../../database/client'
import { clients } from '../../../../database/schema'
import { requireAuth } from '../../../../utils/auth'
import { notifyClient, notifyWarning } from '../../../../utils/bot-events'
import { ensureDeviceWg, syncWireguardConfig } from '../../../../utils/wireguard'
import { caReady, ensureDeviceOvpn } from '../../../../utils/openvpn'
import { ensureDeviceIkev2, syncIkev2 } from '../../../../utils/ikev2'
import { syncClientPrefsFile } from '../../../../utils/client-prefs'
import { readBodyAs } from '../../../../utils/validate'
import { createDeviceWithAddresses } from '../../../../utils/create-device'

const Body = z.object({ name: z.string().trim().min(1).max(64) })

/** Добавить девайс клиенту. Сразу выдаёт ключи WG/OVPN. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(clientId)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  const body = await readBodyAs(event, Body)
  const db = useDb()

  const [client] = await db.select().from(clients).where(eq(clients.id, clientId)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  const name = body.name.trim()
  const cfg = useRuntimeConfig()
  const ovpnReady = cfg.ovpnEnabled && await caReady()
  // Лимит берётся из строки клиента внутри транзакции вставки: прочитанный
  // выше мог устареть, пока шли await (параллельный PATCH лимита). Оттуда
  // же адресат уведомления: бота могли отвязать.
  let owner = client
  const device = await createDeviceWithAddresses(clientId, name, { wg: cfg.wgEnabled, ovpn: ovpnReady }, (names, fresh) => {
    owner = fresh
    if (names.includes(name)) {
      throw createError({ statusCode: 409, statusMessage: 'device_exists', message: `Девайс «${name}» у клиента уже есть` })
    }
    if (fresh.deviceLimit != null && names.length >= fresh.deviceLimit) {
      throw createError({ statusCode: 409, statusMessage: 'device_limit', message: `Достигнут лимит девайсов (${fresh.deviceLimit})` })
    }
  })

  // Сразу минтим ключи, чтобы девайс был готов к выдаче конфигов.
  if (cfg.wgEnabled) {
    await ensureDeviceWg(device.id).catch(err => useLogger().error({ err }, 'ensureDeviceWg on add failed'))
  }
  if (ovpnReady) {
    await ensureDeviceOvpn(device.id).catch(err => useLogger().error({ err }, 'ensureDeviceOvpn on add failed'))
  }
  await ensureDeviceIkev2(device.id).catch(err => useLogger().error({ err }, 'ensureDeviceIkev2 on add failed'))
  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after device add failed'))
  await syncIkev2().catch(err => useLogger().error({ err }, 'ikev2 sync after device add failed'))
  await syncClientPrefsFile().catch(err => useLogger().error({ err }, 'client prefs sync failed'))

  // Уведомить привязанного клиента — устройство добавил администратор.
  // Устройство уже создано: сбой очереди бота — предупреждение, не ошибка.
  const warning = owner.tgChatId
    ? await notifyWarning(notifyClient(
        owner.tgChatId,
        `➕ Администратор добавил вам устройство «${name}». `
        + 'Откройте «📱 Мои устройства», чтобы получить конфиг.',
        owner.name,
      ))
    : undefined

  return { id: device.id, clientId, name: device.name, createdAt: device.createdAt, ...(warning ? { warning } : {}) }
})
