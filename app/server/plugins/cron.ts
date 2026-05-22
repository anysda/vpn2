import { syncWireguardConfig } from '../utils/wireguard'
import { syncOpenvpnConfig } from '../utils/openvpn'
import { collectTraffic } from '../utils/traffic-collector'

const INTERVAL_MS = 60_000

export default defineNitroPlugin(() => {
  const log = useLogger()

  async function tick() {
    // Статус клиента вычисляемый (frozenManual + срок) — отдельного
    // «истечения» в БД нет. Пере-синхроним WG/OVPN каждый тик: девайсы
    // клиентов, у которых истёк срок, выпадут из wg0.conf / попадут в
    // CCD-disable в пределах одного интервала.
    await syncWireguardConfig().catch(err =>
      log.error({ err }, 'cron: wg sync failed'),
    )
    await syncOpenvpnConfig().catch(err =>
      log.error({ err }, 'cron: openvpn sync failed'),
    )

    // Накопительный трафик по всем протоколам (WG+OpenVPN), per-device.
    await collectTraffic().catch(err =>
      log.error({ err }, 'cron: traffic collection failed'),
    )
  }

  // Delay first tick so init plugin has time to run migrations. Without this
  // the first tick races and fails with "no such table" on a fresh DB.
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
