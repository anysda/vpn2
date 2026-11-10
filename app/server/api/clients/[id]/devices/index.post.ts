import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../../../database/client'
import { clients } from '../../../../database/schema'
import { requireAuth } from '../../../../utils/auth'
import { notifyClient, notifyWarning } from '../../../../utils/bot-events'
import { caReady } from '../../../../utils/openvpn'
import { syncClientPrefsFile } from '../../../../utils/client-prefs'
import { readBodyAs } from '../../../../utils/validate'
import { createDeviceWithAddresses, deviceRequestKey, provisionDeviceKeys, rejectDeletedDeviceRepeat } from '../../../../utils/create-device'

const Body = z.object({ name: z.string().trim().min(1).max(64) })

/** Добавить девайс клиенту. Сразу выдаёт ключи WG/OVPN. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(clientId)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  const body = await readBodyAs(event, Body)
  const name = body.name.trim()
  const idempotency = deviceRequestKey(event, { source: 'panel', clientId, name })
  await rejectDeletedDeviceRepeat(idempotency)

  const [client] = await useDb().select().from(clients).where(eq(clients.id, clientId)).limit(1)
  if (!client) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  const cfg = useRuntimeConfig()
  const ovpnReady = cfg.ovpnEnabled && await caReady()
  // Лимит берётся из строки клиента внутри транзакции вставки: прочитанный
  // выше мог устареть, пока шли await (параллельный PATCH лимита). Оттуда
  // же адресат уведомления: бота могли отвязать.
  let owner = client
  const { device, replay } = await createDeviceWithAddresses(clientId, name, { wg: cfg.wgEnabled, ovpn: ovpnReady }, (names, fresh) => {
    owner = fresh
    if (names.includes(name)) {
      throw createError({ statusCode: 409, statusMessage: 'device_exists', message: `Девайс «${name}» у клиента уже есть` })
    }
    if (fresh.deviceLimit != null && names.length >= fresh.deviceLimit) {
      throw createError({ statusCode: 409, statusMessage: 'device_limit', message: `Достигнут лимит девайсов (${fresh.deviceLimit})` })
    }
  }, idempotency)

  // Сразу минтим ключи, чтобы девайс был готов к выдаче конфигов.
  await provisionDeviceKeys(device.id, ovpnReady, {
    wg: 'ensureDeviceWg on add failed',
    ovpn: 'ensureDeviceOvpn on add failed',
    ikev2: 'ensureDeviceIkev2 on add failed',
    wgSync: 'wg sync after device add failed',
    ikev2Sync: 'ikev2 sync after device add failed',
  })
  await syncClientPrefsFile().catch(err => useLogger().error({ err }, 'client prefs sync failed'))

  // Уведомить привязанного клиента — устройство добавил администратор.
  // Устройство уже создано: сбой очереди бота — предупреждение, не ошибка.
  // Повтор по ключу идемпотентности клиента уже уведомил.
  const warning = !replay && owner.tgChatId
    ? await notifyWarning(notifyClient(
        owner.tgChatId,
        `➕ Администратор добавил вам устройство «${name}». `
        + 'Откройте «📱 Мои устройства», чтобы получить конфиг.',
        owner.name,
      ))
    : undefined

  return { id: device.id, clientId, name: device.name, createdAt: device.createdAt, ...(warning ? { warning } : {}) }
})
