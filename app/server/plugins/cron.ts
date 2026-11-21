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
    // Файлы роутера из базы: правка, не дошедшая из-за деплоя или сбоя службы, доезжает здесь.
    if (await deployIdle()) {
      await syncClientPrefsFile().catch(err => log.error({ err }, 'cron: client prefs sync failed'))
      await healRoutesFile().catch(err => log.error({ err }, 'cron: routes sync failed'))
    }
    await collectTraffic().catch(err => log.error({ err }, 'cron: traffic collection failed'))
  }

  // Состав экзитов сменился (задание менеджера): закрепления удалённого экзита снимаются, как только деплой кончился.
  let exitSet = ''
  every(nitroApp, 5_000, async () => {
    const now = JSON.stringify(exitNodes())
    if (now === exitSet || !await deployIdle()) return
    await syncClientPrefsFile()
      .then(() => { exitSet = now })
      .catch(err => log.error({ err }, 'cron: client prefs sync after exit change failed'))
  })

  // Первый тик с задержкой: init должен успеть проверить базу.
  const firstTick = setTimeout(runTick, 5_000)

  const handle = setInterval(runTick, INTERVAL_MS)

  nitroApp.hooks.hook('close', async () => {
    clearTimeout(firstTick)
    clearInterval(handle)
    await running
  })
})
