import type { Page } from '@playwright/test'
import { expect } from './fixtures'
import { openHome } from './clients'
import type { BotDelivery } from '../../shared/bot-delivery'

export async function checkBotQueue(page: Page, checkLayout?: () => Promise<void>) {
  let rows: BotDelivery[] = []
  let polls = 0
  await page.route('**/api/admin/bot-deliveries', (route) => {
    polls++
    return route.fulfill({ json: rows })
  })
  await openHome(page)
  await expect.poll(() => polls).toBeGreaterThan(0)
  const toasts = page.getByRole('region', { includeHidden: true }).locator('ol > li')
  const queued = (count: number): BotDelivery[] => Array.from({ length: count }, (_, i) => ({
    id: `queue-${i}`, label: `Уведомление ${i + 1}`, status: 'queued', remaining: 5,
  }))
  rows = queued(1)
  await expect(toasts).toHaveCount(1)
  await expect(toasts).toContainText('Бот недоступен, 1 уведомлений в очереди')
  rows = queued(5)
  await expect(toasts).toHaveCount(1)
  await expect(toasts).toContainText('Бот недоступен, 5 уведомлений в очереди')
  const start = polls
  await expect.poll(() => polls, { timeout: 15_000 }).toBeGreaterThanOrEqual(start + 4)
  await expect(toasts).toHaveCount(1)
  await expect(toasts).toBeVisible()
  await checkLayout?.()
  console.log('Бот недоступен, 5 уведомлений в очереди — тостов: 1')
  await page.getByRole('button', { name: /^Уведомления:/ }).click()
  await expect(page.getByRole('dialog').getByText('Бот недоступен, 5 уведомлений в очереди', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  rows = rows.map(row => ({ ...row, status: 'accepted', remaining: 0 }))
  await expect(toasts).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^Уведомления:/ })).toHaveCount(0)
  console.log('Бот вернулся, очередь доставлена — тостов: 0')
  // Открытие страницы с уже существующей очередью тоже показывает один тост.
  rows = queued(5)
  await page.reload()
  await expect(toasts).toHaveCount(1)
  await expect(toasts).toContainText('Бот недоступен, 5 уведомлений в очереди')
  rows = []
  await expect(toasts).toHaveCount(0)
}
