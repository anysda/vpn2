import { and, eq, isNotNull, lt } from 'drizzle-orm'
import { useDb } from '../database/client'
import { clients, oneTimeLinks } from '../database/schema'
import { syncShadowsocksConfig } from '../utils/shadowsocks'
import { collectTraffic } from '../utils/traffic-collector'

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

    // Накопительный трафик по всем протоколам (SS+WG+OpenVPN).
    await collectTraffic().catch(err =>
      log.error({ err }, 'cron: traffic collection failed'),
    )
  }

  // Delay first tick so init plugin has time to run migrations. Without this
  // the first tick races and fails with "no such table: clients" on a fresh DB.
  const FIRST_TICK_DELAY_MS = 5_000

  setTimeout(() => {
    tick().catch(err => log.error({ err }, 'cron: initial tick failed'))
  }, FIRST_TICK_DELAY_MS)

  const handle = setInterval(() => {
    tick().catch(err => log.error({ err }, 'cron: tick failed'))
  }, INTERVAL_MS)

  if (typeof process !== 'undefined') {
    process.on('beforeExit', () => clearInterval(handle))
  }
})
