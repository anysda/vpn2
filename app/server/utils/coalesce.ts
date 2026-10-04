/**
 * Схлопывает параллельные вызовы перегенерации конфига в последовательные.
 *
 * Пока прогон идёт, новые вызовы ждут ОДИН следующий прогон, который стартует
 * после текущего и читает БД заново. Так файл не пишут два прогона сразу
 * (общий tmp-файл → ENOENT на rename), и последним на диск не ляжет снимок,
 * прочитанный раньше соседнего, — иначе свежий пир пропадал бы до следующей
 * синхронизации. Каждый вызвавший дожидается прогона, начатого после его вызова.
 */
export function coalesceRuns(fn: () => Promise<void>): () => Promise<void> {
  let running: Promise<void> | null = null
  let next: Promise<void> | null = null

  const start = (): Promise<void> => {
    const p = fn().finally(() => {
      if (running === p) running = null
    })
    running = p
    return p
  }

  return () => {
    if (!running) return start()
    if (!next) {
      next = running.catch(() => {}).then(() => {
        next = null
        return start()
      })
    }
    return next
  }
}
