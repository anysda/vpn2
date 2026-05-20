import { eq } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { syncShadowsocksConfig } from '../../utils/shadowsocks'
import { syncWireguardConfig } from '../../utils/wireguard'
import { caReady, ovpnCn, revokeClientCert, setCcdDisabled } from '../../utils/openvpn'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const db = useDb()
  const result = await db.delete(clients).where(eq(clients.id, id)).returning()
  if (result.length === 0) {
    throw createError({ statusCode: 404, statusMessage: 'not_found' })
  }
  const removed = result[0]!

  await syncShadowsocksConfig().catch((err) => {
    useLogger().error({ err }, 'failed to sync ss config after delete')
  })
  await syncWireguardConfig().catch((err) => {
    useLogger().error({ err }, 'failed to sync wg config after delete')
  })
  // OpenVPN: a deleted client's cert stays cryptographically valid until
  // revoked — push it into the CRL, and drop any stale CCD file.
  if (removed.ovpnCert && await caReady()) {
    await revokeClientCert(removed.ovpnCert).catch((err) => {
      useLogger().error({ err }, 'failed to revoke ovpn cert after delete')
    })
    await setCcdDisabled(ovpnCn(removed.id), false).catch(() => {})
  }

  return { ok: true }
})
