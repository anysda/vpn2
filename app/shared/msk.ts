export const MSK = 'Europe/Moscow'
export const DAY_MS = 86_400_000
/** В МСК нет перехода на летнее время: сдвиг всегда +3 ч. */
export const MSK_OFFSET_SEC = 3 * 3600

/** «31.12.2026» по Москве. */
export const mskDate = (at: Date | number) => new Date(at).toLocaleDateString('ru-RU', { timeZone: MSK })
