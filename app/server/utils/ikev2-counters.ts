/** Байт-счётчики живых IKE_SA: «<deviceId>:<uniqueid>» → [rx, tx]. */
export type SaCounters = Record<string, [number, number]>

export interface SaSample { deviceId: number, uniqueid: string, rx: number, tx: number }

/**
 * Дельты IKEv2 относительно прошлого снимка. Счётчик живёт, пока живёт IKE_SA:
 * новую SA засчитываем целиком, пережившую снимок - разницей. Снимок хранится
 * в базе и переживает рестарт панели. prev = null - снимка ещё не было, только базируемся.
 */
export function ikev2Deltas(prev: SaCounters | null, samples: SaSample[]) {
  const next: SaCounters = {}
  const deltas: Array<{ deviceId: number, rx: number, tx: number }> = []
  for (const s of samples) {
    const key = `${s.deviceId}:${s.uniqueid}`
    next[key] = [s.rx, s.tx]
    if (!prev) continue
    const [lastRx, lastTx] = prev[key] ?? [0, 0]
    // Меньше прошлого: тот же uniqueid у новой SA (перезапуск charon).
    const rx = s.rx >= lastRx ? s.rx - lastRx : s.rx
    const tx = s.tx >= lastTx ? s.tx - lastTx : s.tx
    if (rx > 0 || tx > 0) deltas.push({ deviceId: s.deviceId, rx, tx })
  }
  // Исчезнувшие SA из снимка уходят: после перезапуска charon uniqueid начинаются заново.
  return { deltas, next }
}
