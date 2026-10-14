import { pluralRu } from './plural'

export interface BotDelivery {
  id: string
  label: string
  status: 'queued' | 'accepted' | 'failed'
  remaining: number
  /** Постановка в очередь, мс. */
  created: number
}

const MSK = 'Europe/Moscow'
const day = new Intl.DateTimeFormat('en-CA', { timeZone: MSK })
const clock = new Intl.DateTimeFormat('ru-RU', { timeZone: MSK, hour: '2-digit', minute: '2-digit' })
const date = new Intl.DateTimeFormat('ru-RU', { timeZone: MSK, day: '2-digit', month: '2-digit' })

/** Время строки колокольчика по МСК: «сегодня 14:31», «вчера 23:30». */
export function botDeliveryTime(created: number, now = Date.now()): string {
  const when = day.format(created)
  if (when === day.format(now)) return `сегодня ${clock.format(created)}`
  // В МСК нет перехода на летнее время: сутки всегда 24 часа.
  if (when === day.format(now - 86_400_000)) return `вчера ${clock.format(created)}`
  return `${date.format(created)} ${clock.format(created)}`
}

export function botQueueText(count: number): string {
  return `Бот недоступен, ${count} ${pluralRu(count, ['уведомление', 'уведомления', 'уведомлений'])} в очереди`
}

export function botDeliveryText(row: BotDelivery): string {
  if (row.status === 'accepted') return `${row.label}: передано боту.`
  if (row.status === 'failed') return `${row.label}: доставка не удалась. Повторов не будет.`
  return `${row.label}: не отправлено. Ещё попыток: ${row.remaining}.`
}
