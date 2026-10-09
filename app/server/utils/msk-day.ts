// Граница суток панели — 00:00 по Москве (UTC+3 круглый год, без перехода на
// летнее время). Одна на всё: суточный трафик нод (vm-client) и клиентов
// (client-activity) обязаны обнуляться в один и тот же момент.
export const MSK_OFFSET_SEC = 3 * 3600

/** Номер московских суток (дни от эпохи). */
export const mskDay = (ms: number) => Math.floor((ms / 1000 + MSK_OFFSET_SEC) / 86400)

/** Unix-секунды начала московских суток, `daysBack` суток назад от текущих. */
export const mskDayStartSec = (ms: number, daysBack = 0) =>
  (mskDay(ms) - daysBack) * 86400 - MSK_OFFSET_SEC
