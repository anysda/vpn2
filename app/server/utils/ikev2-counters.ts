/** Байт-счётчики живых IKE_SA: «<deviceId>:<uniqueid>» → [rx, tx]. */
export type SaCounters = Record<string, [number, number]>

export interface SaSample {
  deviceId: number
  uniqueid: string
  rx: number
  tx: number
}

/**
 * Дельты IKEv2 относительно прошлого снимка.
 *
 * Счётчик живёт, пока живёт IKE_SA: переподключение клиента — новая SA с
 * новым uniqueid и счётчиком с нуля. Поэтому SA, которой нет в прошлом снимке,
 * засчитываем целиком, а пережившую его — разницей. Снимок хранится в БД, и
 * после рестарта панели SA продолжает считаться от сохранённых значений: не
 * засчитывается заново и не теряет то, что набрала, пока панель лежала.
 *
 * prev = null — снимка ещё не было ни разу (первый запуск с этим кодом):
 * сколько из текущих счётчиков уже учтено, неизвестно, только базируемся.
 */
export function ikev2Deltas(prev: SaCounters | null, samples: SaSample[]) {
  const next: SaCounters = {}
  const deltas: Array<{ deviceId: number, rx: number, tx: number }> = []
  for (const s of samples) {
    const key = `${s.deviceId}:${s.uniqueid}`
    next[key] = [s.rx, s.tx]
    if (!prev) continue
    const [lastRx, lastTx] = prev[key] ?? [0, 0]
    // меньше прошлого — тот же uniqueid у новой SA (перезапуск charon)
    const rx = s.rx >= lastRx ? s.rx - lastRx : s.rx
    const tx = s.tx >= lastTx ? s.tx - lastTx : s.tx
    if (rx > 0 || tx > 0) deltas.push({ deviceId: s.deviceId, rx, tx })
  }
  // SA, которых больше нет, из снимка уходят: uniqueid после перезапуска
  // charon начинаются заново, и новая SA не должна считаться от старой.
  return { deltas, next }
}
