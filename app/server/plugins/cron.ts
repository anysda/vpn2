import { and, eq, isNotNull, lt } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients, oneTimeLinks } from '../database/schema'
import { syncShadowsocksConfig } from '../utils/shadowsocks'

const INTERVAL_MS = 60_000
const OTL_GRACE_MS = 60_000

export default defineNitroPlugin(() => {
  const log = useLogger()

  async function tick() {
    const db = useDb()
    const now = new Date()

    const expired = await db
      .update(clients)
      .set({ enabled: false, updatedAt: now })
      .where(
        and(eq(clients.enabled, true), isNotNull(clients.expiresAt), lt(clients.expiresAt, now)),
      )
      .returning({ id: clients.id })

    if (expired.length > 0) {
      log.info({ count: expired.length }, 'cron: clients expired')
      await syncShadowsocksConfig().catch(err =>
        log.error({ err }, 'cron: ss sync after expire failed'),
      )
    }

    const cleanupThreshold = new Date(now.getTime() - OTL_GRACE_MS)
    const deleted = await db
      .delete(oneTimeLinks)
      .where(lt(oneTimeLinks.expiresAt, cleanupThreshold))
      .returning({ id: oneTimeLinks.id })

    if (deleted.length > 0) {
      log.debug({ count: deleted.length }, 'cron: OTLs cleaned')
    }
  }

  tick().catch(err => log.error({ err }, 'cron: initial tick failed'))

  const handle = setInterval(() => {
    tick().catch(err => log.error({ err }, 'cron: tick failed'))
  }, INTERVAL_MS)

  if (typeof process !== 'undefined') {
    process.on('beforeExit', () => clearInterval(handle))
  }
})
