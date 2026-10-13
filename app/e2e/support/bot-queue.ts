import type { Page } from '@playwright/test'
import { expect, hydrated } from './fixtures'
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
  await expect(toasts).toContainText('Бот недоступен, 1 уведомление в очереди')
  await checkLayout?.()
  rows = queued(5)
  await expect(page.getByRole('button', { name: 'Уведомления: 5', exact: true })).toBeVisible()
  // Очередь не доставлена: тост должен погаснуть сам за несколько секунд.
  await page.mouse.move(0, 0)
  await expect(toasts, 'непустая очередь не оставляет вечный тост').toHaveCount(0)
  await page.getByRole('button', { name: 'Уведомления: 5', exact: true }).click()
  await expect(page.getByRole('dialog').getByText('Бот недоступен, 5 уведомлений в очереди', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  console.log('Бот недоступен, 5 уведомлений в очереди — в «Уведомлениях»; тост погас сам')
  rows = queued(7)
  await expect(page.getByRole('button', { name: 'Уведомления: 7', exact: true })).toBeVisible()
  await expect(toasts, 'изменение счётчика не повторяет тост').toHaveCount(0)
  await page.reload()
  await hydrated(page)
  await expect(page.getByRole('button', { name: 'Уведомления: 7', exact: true })).toBeVisible()
  await expect(toasts, 'перезагрузка сохраняет сессию вкладки').toHaveCount(0)
  rows = rows.map(row => ({ ...row, status: 'accepted', remaining: 0 }))
  await expect(page.getByRole('button', { name: /^Уведомления:/ })).toHaveCount(0)
  rows = queued(5)
  await expect(page.getByRole('button', { name: 'Уведомления: 5', exact: true })).toBeVisible()
  await expect(toasts, 'новая очередь в той же сессии не повторяет тост').toHaveCount(0)
  console.log('Счётчик изменён, страница перезагружена, очередь заполнена снова — тостов: 0')

  // Вкладка разделяет авторизацию, но имеет собственную сессию уведомления.
  const tab = await page.context().newPage()
  try {
    await tab.route('**/api/admin/bot-deliveries', route => route.fulfill({ json: rows }))
    await tab.goto('/')
    await hydrated(tab)
    const newToasts = tab.getByRole('region', { includeHidden: true }).locator('ol > li')
    await expect(newToasts).toHaveCount(1)
    await expect(newToasts).toContainText('Бот недоступен, 5 уведомлений в очереди')
    await newToasts.getByRole('button', { name: /Close|Закрыть/ }).click()
    await expect(newToasts).toHaveCount(0)
    rows = queued(7)
    await expect(tab.getByRole('button', { name: 'Уведомления: 7', exact: true })).toBeVisible()
    await expect(newToasts, 'закрытый вручную тост не возвращается').toHaveCount(0)
    await tab.reload()
    await hydrated(tab)
    await expect(tab.getByRole('button', { name: 'Уведомления: 7', exact: true })).toBeVisible()
    await expect(newToasts).toHaveCount(0)
    console.log('Новая вкладка — один тост; после закрытия, изменения счётчика и перезагрузки — тостов: 0')
  }
  finally { await tab.close() }
}
