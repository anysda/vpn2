import { z } from 'zod'
import { botClientView, clientByChat, requireBotAuth } from '../../../utils/bot-api'
import { ensureDeviceWg, syncWireguardConfig } from '../../../utils/wireguard'
import { caReady, ensureDeviceOvpn } from '../../../utils/openvpn'
import { ensureDeviceIkev2, syncIkev2 } from '../../../utils/ikev2'
import { readBodyAs } from '../../../utils/validate'
import { createDeviceWithAddresses } from '../../../utils/create-device'

const Body = z.object({
  chatId: z.number().int(),
  name: z.string().min(1).max(64),
})

// Клиент добавляет себе девайс через бота.
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const body = await readBodyAs(event, Body)
  const client = await clientByChat(body.chatId)

  const name = body.name.trim()
  const cfg = useRuntimeConfig()
  const ovpnReady = cfg.ovpnEnabled && await caReady()
  const device = await createDeviceWithAddresses(client.id, name, { wg: cfg.wgEnabled, ovpn: ovpnReady }, (names) => {
    if (names.includes(name)) {
      throw createError({ statusCode: 409, statusMessage: 'device_exists', message: 'Девайс с таким названием уже есть' })
    }
    if (client.deviceLimit != null && names.length >= client.deviceLimit) {
      throw createError({ statusCode: 409, statusMessage: 'device_limit', message: 'Достигнут лимит устройств — обратитесь к администратору' })
    }
  })
  if (cfg.wgEnabled) {
    await ensureDeviceWg(device.id).catch(err => useLogger().error({ err }, 'bot: ensureDeviceWg failed'))
  }
  if (ovpnReady) {
    await ensureDeviceOvpn(device.id).catch(err => useLogger().error({ err }, 'bot: ensureDeviceOvpn failed'))
  }
  await ensureDeviceIkev2(device.id).catch(err => useLogger().error({ err }, 'bot: ensureDeviceIkev2 failed'))
  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'bot: wg sync failed'))
  await syncIkev2().catch(err => useLogger().error({ err }, 'bot: ikev2 sync failed'))

  return botClientView(client.id)
})
