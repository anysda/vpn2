import { and, eq } from 'drizzle-orm'
import { useDb } from '../../../../database/client'
import { clients, devices } from '../../../../database/schema'
import { requireAuth } from '../../../../utils/auth'
import { notifyClient } from '../../../../utils/bot-events'
import { syncWireguardConfig } from '../../../../utils/wireguard'
import { caReady, ovpnCn, revokeClientCert, setCcdDisabled } from '../../../../utils/openvpn'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const clientId = Number(getRouterParam(event, 'id'))
  const deviceId = Number(getRouterParam(event, 'deviceId'))
  if (!Number.isFinite(clientId) || !Number.isFinite(deviceId)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const db = useDb()
  const [device] = await db.select().from(devices)
    .where(and(eq(devices.id, deviceId), eq(devices.clientId, clientId)))
    .limit(1)
  if (!device) throw createError({ statusCode: 404, statusMessage: 'not_found' })

  await db.delete(devices).where(eq(devices.id, deviceId))
  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after device delete failed'))

  if (device.ovpnCert && await caReady()) {
    await revokeClientCert(device.ovpnCert).catch(err =>
      useLogger().error({ err }, 'ovpn revoke after device delete failed'))
    await setCcdDisabled(ovpnCn(device.id), false).catch(() => {})
  }

  // Уведомить привязанного клиента — устройство удалил администратор.
  const [client] = await db.select({ tgChatId: clients.tgChatId }).from(clients)
    .where(eq(clients.id, clientId)).limit(1)
  if (client?.tgChatId) {
    notifyClient(
      client.tgChatId,
      `🗑 Администратор удалил ваше устройство «${device.name}» — его конфиг VPN больше не действует.`,
    )
  }

  return { ok: true }
})
