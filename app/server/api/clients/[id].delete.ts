import { eq } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { syncShadowsocksConfig } from '../../utils/shadowsocks'
import { syncWireguardConfig } from '../../utils/wireguard'

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

  await syncShadowsocksConfig().catch((err) => {
    useLogger().error({ err }, 'failed to sync ss config after delete')
  })
  await syncWireguardConfig().catch((err) => {
    useLogger().error({ err }, 'failed to sync wg config after delete')
  })

  return { ok: true }
})
