import { eq } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { clients, devices } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { syncWireguardConfig } from '../../utils/wireguard'
import { caReady, ovpnCn, revokeClientCert, setCcdDisabled } from '../../utils/openvpn'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) throw createError({ statusCode: 400, statusMessage: 'invalid_id' })

  const db = useDb()
  // Девайсы нужны ДО удаления — отозвать их OpenVPN-сертификаты.
  const devRows = await db.select().from(devices).where(eq(devices.clientId, id))

  const result = await db.delete(clients).where(eq(clients.id, id)).returning()
  if (result.length === 0) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  // devices удаляются каскадом (FK ON DELETE CASCADE).

  await syncWireguardConfig().catch(err => useLogger().error({ err }, 'wg sync after client delete failed'))

  if (devRows.length && await caReady()) {
    for (const d of devRows) {
      if (!d.ovpnCert) continue
      await revokeClientCert(d.ovpnCert).catch(err =>
        useLogger().error({ err }, 'ovpn revoke after client delete failed'))
      await setCcdDisabled(ovpnCn(d.id), false).catch(() => {})
    }
  }

  return { ok: true }
})
