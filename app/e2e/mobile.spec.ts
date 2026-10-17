import type { Page } from '@playwright/test'
import { expect, test } from './support/fixtures'
import { openHome } from './support/clients'
import { noHorizontalOverflow } from './support/overflow'
import { TAB_PAGES } from '../app/utils/tab-pages'

/** Верх шапки и низ таб-бара в окне: после прокрутки они должны остаться там же. */
async function chrome(page: Page) {
  const head = await page.locator('.wrap>header').boundingBox()
  const bar = await page.locator('.mbar nav').boundingBox()
  return { head: head?.y, bar: bar && Math.round(bar.y + bar.height) }
}

/** Шторка открыта и закрывается тапом по оверлею над ней. */
async function sheetClosesOnOverlay(page: Page, name: string) {
  const sheet = page.getByRole('dialog', { name })
  await expect(sheet).toBeVisible()
  await page.locator('.bso.on').tap({ position: { x: 40, y: 40 } })
  await expect(sheet).toBeHidden()
}

for (const scheme of ['dark', 'light'] as const) {
  test(`телефон, тема ${scheme}: разделы из таб-бара без горизонтальной прокрутки, шапка и таб-бар на месте`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme })
    await openHome(page)
    const bar = page.locator('.mbar nav')
    await expect(bar).toBeVisible()

    for (const { path, label } of TAB_PAGES) {
      await bar.getByRole('link', { name: label }).tap()
      await expect(page).toHaveURL(u => u.pathname === path)
      await expect(page.locator('.mhead h1')).not.toBeEmpty()
      await noHorizontalOverflow(page, path)

      const top = await chrome(page)
      await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
      await expect.poll(() => chrome(page), `${path}: шапка и таб-бар после прокрутки`).toEqual(top)
      await page.evaluate(() => window.scrollTo(0, 0))
    }

    await page.getByRole('button', { name: /^Меню @/ }).tap()
    await sheetClosesOnOverlay(page, 'Меню профиля')
    await page.getByRole('button', { name: /^Уведомления/ }).tap()
    await sheetClosesOnOverlay(page, 'Доставка уведомлений')
  })
}
