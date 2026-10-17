import type { Page } from '@playwright/test'
import { expect, test } from './support/fixtures'
import { openHome } from './support/clients'
import { noHorizontalOverflow } from './support/overflow'

// Верхнее свечение div.light на телефоне, как в мобильном макете: пятно 620×420
// над верхним краем, шапка его не перекрывает, вбок страница не едет.
test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

function glow(page: Page) {
  return page.evaluate(() => {
    const light = document.querySelector('div.light')!
    const b = getComputedStyle(light, '::before')
    const header = document.querySelector('.wrap>header')
    return {
      display: getComputedStyle(light).display,
      spot: { top: b.top, width: b.width, height: b.height, image: b.backgroundImage.startsWith('radial-gradient') },
      streak: getComputedStyle(light, '::after').display,
      header: header ? getComputedStyle(header).backgroundImage.startsWith('radial-gradient') : null,
    }
  })
}

const spot = { top: '-220px', width: '620px', height: '420px', image: true }

for (const scheme of ['dark', 'light'] as const) {
  test(`свечение на телефоне, тема ${scheme}: по макету, шапка не перекрывает, без прокрутки вбок`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme })
    for (const path of ['/', '/clients', '/routes', '/dns', '/settings']) {
      await openHome(page, path)
      expect(await glow(page), path).toEqual({ display: 'block', spot, streak: 'none', header: true })
      await noHorizontalOverflow(page, `${path}, ${scheme}`)
    }
    await page.context().clearCookies()
    await page.goto('/login')
    await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible()
    expect(await glow(page), '/login').toEqual({ display: 'block', spot, streak: 'none', header: null })
    await noHorizontalOverflow(page, `/login, ${scheme}`)
  })
}
