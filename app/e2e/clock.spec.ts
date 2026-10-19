import { expect, test } from '@playwright/test'
import { pauseClockAt } from './support/clock'

// Под нагрузкой между установкой часов и паузой проходит время; пауза в
// точке установки тогда падает с «Cannot fast-forward to the past».
test('часы встают на паузу в заданной точке и после задержки между вызовами', async ({ page }) => {
  const at = 1_800_000_000_000
  await pauseClockAt(page, at, 1_500)
  expect(await page.evaluate(() => Date.now())).toBe(at)
})
