/**
 * Начало завтрашнего дня (UTC) в миллисекундах — минимально допустимый
 * срок действия клиента. Дату раньше завтрашней ставить нельзя.
 */
export function minExpiryMs(): number {
  const d = new Date()
  d.setUTCHours(0, 0, 0, 0)
  d.setUTCDate(d.getUTCDate() + 1)
  return d.getTime()
}
