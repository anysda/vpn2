import { pluralRu } from './plural'

export interface BotDelivery {
  id: string
  label: string
  status: 'queued' | 'accepted' | 'failed'
  remaining: number
}

export function botQueueText(count: number): string {
  return `Бот недоступен, ${count} ${pluralRu(count, ['уведомление', 'уведомления', 'уведомлений'])} в очереди`
}

export function botDeliveryText(row: BotDelivery): string {
  if (row.status === 'accepted') return `${row.label}: передано боту.`
  if (row.status === 'failed') return `${row.label}: доставка не удалась. Повторов не будет.`
  return `${row.label}: не отправлено. Ещё попыток: ${row.remaining}.`
}
