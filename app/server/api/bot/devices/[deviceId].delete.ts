import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../database/client'
import { devices } from '../../../database/schema'
import { botClientView, clientByChat, requireBotAuth } from '../../../utils/bot-api'
import { syncWireguardConfig } from '../../../utils/wireguard'
import { caReady, ovpnCn, revokeClientCert, setCcdDisabled } from '../../../utils/openvpn'
import { syncIkev2, terminateIkev2Sa } from '../../../utils/ikev2'

// Клиент удаляет свой девайс через бота.
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  const chatId = Number(getQuery(event).chatId)
  if (!Number.isFinite(deviceId) || !Number.isFinite(chatId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_params' })
  }
  const client = await clientByChat(chatId)
  const db = useDb()

  const [device] = await db.select().from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, client.id)))
    .limit(1)
  if (!device) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  await db.delete(devices).where(eq(devices.id, deviceId))
  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'bot: wg sync after delete failed'))
  if (device.ovpnCert && await caReady()) {
    await revokeClientCert(device.ovpnCert).catch(err => useLogger().error({ err }, 'bot: ovpn revoke failed'))
    await setCcdDisabled(ovpnCn(device.id), false).catch(() => {})
  }
  if (device.ikev2Username) {
    await terminateIkev2Sa(device.ikev2Username).catch(err =>
      useLogger().warn({ err: (err as Error).message }, 'bot: ikev2 terminate after delete skipped'))
  }
  await syncIkev2().catch(err => useLogger().error({ err }, 'bot: ikev2 sync after delete failed'))

  return botClientView(client.id)
})
