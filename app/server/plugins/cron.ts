import { enqueueExpiredAccess, runAccessOperations } from '../utils/access-operations'
import { collectTraffic } from '../utils/traffic-collector'
import { reportControlExpiry, syncControlExpiry } from '../utils/expiry'
import { syncClientPrefsFile } from '../utils/client-prefs'

const INTERVAL_MS = 60_000

export default defineNitroPlugin((nitroApp) => {
  const log = useLogger()
  let running: Promise<void> | null = null

  function runTick() {
    // Медленный тик не накапливает параллельные.
    if (running) return
    running = tick()
      .catch(err => log.error({ err }, 'cron: tick failed'))
      .finally(() => { running = null })
  }

  async function tick() {
    await enqueueExpiredAccess()
    await runAccessOperations()
    await reportControlExpiry().catch(err => log.error({ err }, 'cron: control expiry report failed'))
    await syncControlExpiry().catch(err => log.error({ err }, 'cron: control expiry sync failed'))
    await syncClientPrefsFile().catch(err => log.error({ err }, 'cron: client prefs sync failed'))
    await collectTraffic().catch(err => log.error({ err }, 'cron: traffic collection failed'))
  }

  // Первый тик с задержкой: init должен успеть проверить базу.
  const firstTick = setTimeout(runTick, 5_000)

  const handle = setInterval(runTick, INTERVAL_MS)

  nitroApp.hooks.hook('close', async () => {
    clearTimeout(firstTick)
    clearInterval(handle)
    await running
  })
})
