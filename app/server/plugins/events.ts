import { pruneEvents } from '../utils/events'
import { NODE_CHECK_MS, checkFailover, checkNodes } from '../utils/node-watch'
import { isPanelReady } from '../utils/panel-ready'

const PRUNE_MS = 3600_000

// Журнал событий: сторож нод и failover раз в 30 с, чистка старше 30 дней раз в час.
export default defineNitroPlugin((nitroApp) => {
  const log = useLogger()
  let running: Promise<void> | null = null
  let prunedAt = 0

  async function tick() {
    await checkNodes().catch(err => log.debug({ err }, 'events: node check skipped'))
    await checkFailover().catch(err => log.warn({ err }, 'events: failover check failed'))
    if (Date.now() - prunedAt >= PRUNE_MS) {
      prunedAt = Date.now()
      await pruneEvents().catch(err => log.error({ err }, 'events: prune failed'))
    }
  }

  function runTick() {
    // До миграций таблицы журнала может не быть.
    if (running || !isPanelReady()) return
    running = tick().finally(() => { running = null })
  }

  const handle = setInterval(runTick, NODE_CHECK_MS)
  const firstTick = setTimeout(runTick, 5_000)

  nitroApp.hooks.hook('close', async () => {
    clearTimeout(firstTick)
    clearInterval(handle)
    await running
  })
})
