import { enqueueExpiredAccess, runAccessOperations } from '../utils/access-operations'
import { collectTraffic } from '../utils/traffic-collector'
import { reportControlExpiry, syncControlExpiry } from '../utils/expiry'
import { syncClientPrefsFile } from '../utils/client-prefs'

const INTERVAL_MS = 60_000

export default defineNitroPlugin((nitroApp) => {
  const log = useLogger()
  let running: Promise<void> | null = null

  function runTick() {
    // Медленный helper не должен накапливать параллельные тики и записи
    // счётчиков. Следующий интервал перечитает актуальное состояние.
    if (running) return
    running = tick()
      .catch(err => log.error({ err }, 'cron: tick failed'))
      .finally(() => { running = null })
  }

  async function tick() {
    await enqueueExpiredAccess()
    await runAccessOperations()

    // Что сервис снял по сроку сам (панель стояла) — в журнал один раз.
    await reportControlExpiry().catch(err =>
      log.error({ err }, 'cron: control expiry report failed'),
    )
    await syncControlExpiry().catch(err =>
      log.error({ err }, 'cron: control expiry sync failed'),
    )

    // Предпочитаемые экзиты: самоисцеление файла (устройства, добавленные
    // ботом, удалённые клиенты). Без изменений файл не переписывается.
    await syncClientPrefsFile().catch(err =>
      log.error({ err }, 'cron: client prefs sync failed'),
    )

    // Накопительный трафик по всем протоколам (WG+OpenVPN+IKEv2), per-device.
    await collectTraffic().catch(err =>
      log.error({ err }, 'cron: traffic collection failed'),
    )
  }

  // Delay first tick so init plugin has time to run migrations. Without this
  // the first tick races and fails with "no such table" on a fresh DB.
  const FIRST_TICK_DELAY_MS = 5_000

  const firstTick = setTimeout(runTick, FIRST_TICK_DELAY_MS)
  const handle = setInterval(runTick, INTERVAL_MS)

  nitroApp.hooks.hook('close', async () => {
    clearTimeout(firstTick)
    clearInterval(handle)
    await running
  })
})
