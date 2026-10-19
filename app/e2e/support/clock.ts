import type { Page } from '@playwright/test'

/**
 * Запас между установкой часов и паузой. После `clock.install` время страницы
 * идёт дальше, и `pauseAt` в прошлое падает («Cannot fast-forward to the past»):
 * под нагрузкой между двумя вызовами проходят миллисекунды, а то и секунды.
 */
export const CLOCK_MARGIN_MS = 10_000

/** Часы страницы стоят ровно на `at`. `gap` — задержка между вызовами, для проверки самого запаса. */
export async function pauseClockAt(page: Page, at: number, gap = 0) {
  await page.clock.install({ time: at - CLOCK_MARGIN_MS })
  if (gap) await new Promise(resolve => setTimeout(resolve, gap))
  await page.clock.pauseAt(at)
}
