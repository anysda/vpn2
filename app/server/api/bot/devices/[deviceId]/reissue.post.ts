import { and, eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../../../database/client'
import { devices } from '../../../../database/schema'
import { botClientView, clientByChat, requireBotAuth } from '../../../../utils/bot-api'
import { reissueDeviceWg, syncWireguardConfig } from '../../../../utils/wireguard'
import { reissueDeviceOvpn, syncOpenvpnConfig } from '../../../../utils/openvpn'

const Body = z.object({ chatId: z.number().int() })

// Клиент перевыпускает ключи своего девайса через бота.
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!Number.isFinite(deviceId)) throw createError({ statusCode: 400, statusMessage: 'invalid_params' })
  const body = await readValidatedBody(event, Body.parse)
  const client = await clientByChat(body.chatId)
  const db = useDb()

  const [device] = await db.select({ id: devices.id }).from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, client.id)))
    .limit(1)
  if (!device) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  await reissueDeviceWg(deviceId)
  await reissueDeviceOvpn(deviceId)
  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'bot: wg sync after reissue failed'))
  await syncOpenvpnConfig().catch(err => useLogger().error({ err }, 'bot: ovpn sync after reissue failed'))

  return botClientView(client.id)
})
