import { z } from 'zod'
import { botClientView, clientByChat, requireBotAuth } from '../../../utils/bot-api'
import { caReady } from '../../../utils/openvpn'
import { syncClientPrefsFile } from '../../../utils/client-prefs'
import { readBodyAs } from '../../../utils/validate'
import { createDeviceWithAddresses, deviceRequestKey, provisionDeviceKeys, rejectDeletedDeviceRepeat } from '../../../utils/create-device'

const Body = z.object({
  chatId: z.number().int(),
  name: z.string().trim().min(1).max(64),
})

// Клиент добавляет себе девайс через бота.
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const body = await readBodyAs(event, Body)
  const name = body.name
  const idempotency = deviceRequestKey(event, { source: 'bot', chatId: body.chatId, name })
  await rejectDeletedDeviceRepeat(idempotency)
  const client = await clientByChat(body.chatId)

  const cfg = useRuntimeConfig()
  const ovpnReady = cfg.ovpnEnabled && await caReady()
  // Привязку и лимит перепроверяем в транзакции вставки: пока шли await,
  // администратор мог отозвать бота или снизить лимит.
  const { device } = await createDeviceWithAddresses(client.id, name, { wg: cfg.wgEnabled, ovpn: ovpnReady }, (names, fresh) => {
    if (fresh.tgChatId !== body.chatId) throw createError({ statusCode: 404, statusMessage: 'not_linked' })
    if (names.includes(name)) {
      throw createError({ statusCode: 409, statusMessage: 'device_exists', message: 'Девайс с таким названием уже есть' })
    }
    if (fresh.deviceLimit != null && names.length >= fresh.deviceLimit) {
      throw createError({ statusCode: 409, statusMessage: 'device_limit', message: 'Достигнут лимит устройств — обратитесь к администратору' })
    }
  }, idempotency)
  await provisionDeviceKeys(device.id, ovpnReady, {
    wg: 'bot: ensureDeviceWg failed',
    ovpn: 'bot: ensureDeviceOvpn failed',
    ikev2: 'bot: ensureDeviceIkev2 failed',
    wgSync: 'bot: wg sync failed',
    ikev2Sync: 'bot: ikev2 sync failed',
  })
  await syncClientPrefsFile().catch(err => useLogger().error({ err }, 'bot: client prefs sync failed'))

  return botClientView(client.id)
})
