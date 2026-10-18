import type { Page } from '@playwright/test'
import { expect, hydrated, test, uniq } from './support/fixtures'
import { createClientUi, openHome } from './support/clients'
import { noHorizontalOverflow } from './support/overflow'

// Узкий десктоп сразу над мобильным каркасом (он до 720px включительно): шапка
// с вкладками посередине не шире окна, страница не прокручивается вбок,
// в карточках ничего не обрезано.
const WIDTHS = [721, 760, 800]
const DNS_SUBS = ['Сводка', 'Журнал', 'Фильтры', 'Свои правила', 'Перезаписи', 'Сервисы', 'Клиенты', 'Настройки DNS']

async function profileButtonInside(page: Page, where: string) {
  const width = page.viewportSize()!.width
  const me = await page.locator('header button.me').boundingBox()
  expect(me, `${where}: кнопка профиля`).not.toBeNull()
  expect(Math.round(me!.x), `${where}: левый край кнопки профиля`).toBeGreaterThanOrEqual(0)
  expect(Math.round(me!.x + me!.width), `${where}: правый край кнопки профиля`).toBeLessThanOrEqual(width)
}

for (const width of WIDTHS) {
  test(`окно ${width}px: шапка и разделы без горизонтальной прокрутки`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await openHome(page)
    await expect(page.getByText('загружаю метрики…')).toHaveCount(0)
    await expect(page.getByText('CPU').first()).toBeVisible()
    await noHorizontalOverflow(page, `${width}px /`)
    await profileButtonInside(page, `${width}px /`)
    // Имя скрыто только с глаз: кнопка по-прежнему называется логином.
    await expect(page.getByRole('button', { name: /^@/ })).toBeVisible()

    await page.goto('/clients')
    await hydrated(page)
    await noHorizontalOverflow(page, `${width}px /clients`)
    await profileButtonInside(page, `${width}px /clients`)
    const name = uniq('narrow')
    await createClientUi(page, name)
    await page.getByTestId('client-row').filter({ hasText: name }).click()
    await expect(page.getByTestId('client-detail').getByRole('heading', { name })).toBeVisible()
    await expect(page.getByTestId('client-detail').getByRole('heading', { name })).toBeInViewport()
    await noHorizontalOverflow(page, `${width}px /clients, карточка клиента`)

    await page.goto('/routes')
    await expect(page.getByRole('heading', { level: 1, name: 'Маршруты' })).toBeVisible()
    await noHorizontalOverflow(page, `${width}px /routes`)
    await profileButtonInside(page, `${width}px /routes`)

    await page.goto('/dns')
    await hydrated(page)
    for (const sub of DNS_SUBS) {
      await page.locator('.dns .dseg').getByRole('tab', { name: sub, exact: true }).click()
      await expect(page.locator('.dns .dseg').getByRole('tab', { name: sub, exact: true })).toHaveAttribute('aria-selected', 'true')
      await page.waitForLoadState('networkidle')
      await noHorizontalOverflow(page, `${width}px /dns «${sub}»`)
    }
    await profileButtonInside(page, `${width}px /dns`)

    await page.goto('/settings')
    await expect(page.getByRole('heading', { name: 'Мой профиль' })).toBeVisible()
    await noHorizontalOverflow(page, `${width}px /settings`)
    await profileButtonInside(page, `${width}px /settings`)
  })
}
