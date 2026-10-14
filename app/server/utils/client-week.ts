import { mskDay } from './msk-day'

export interface DayTraffic {
  /** Московская дата суток, ГГГГ-ММ-ДД. */
  date: string
  rx: number
  tx: number
}

/**
 * Почасовой трафик → 7 московских суток, от шести суток назад до сегодня.
 * Час целиком лежит в одних сутках: смещение МСК — ровно 3 часа.
 * Сутки без трафика остаются нулём; часы вне окна отбрасываются.
 */
export function weekByMskDay(rows: { hour: number, rx: number, tx: number }[], nowMs: number): DayTraffic[] {
  const today = mskDay(nowMs)
  const days = Array.from({ length: 7 }, (_, i) => ({
    date: new Date((today - 6 + i) * 86_400_000).toISOString().slice(0, 10),
    rx: 0,
    tx: 0,
  }))
  for (const r of rows) {
    const day = days[6 - (today - mskDay(r.hour * 1000))]
    if (!day) continue
    day.rx += r.rx
    day.tx += r.tx
  }
  return days
}
