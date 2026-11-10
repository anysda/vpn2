import { describe, expect, it } from 'vitest'
import { botQueueText } from './bot-delivery'

describe('botQueueText', () => {
  it.each([
    [1, 'Бот недоступен, 1 уведомление в очереди'],
    [2, 'Бот недоступен, 2 уведомления в очереди'],
    [5, 'Бот недоступен, 5 уведомлений в очереди'],
    [11, 'Бот недоступен, 11 уведомлений в очереди'],
    [21, 'Бот недоступен, 21 уведомление в очереди'],
    [0, 'Бот недоступен, 0 уведомлений в очереди']
  ])('%i', (n, expected) => {
    expect(botQueueText(n)).toBe(expected)
  })
})
