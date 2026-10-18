import type { Page } from '@playwright/test'
import { expect, test, uniq } from './support/fixtures'
import { openHome } from './support/clients'
import { TAB_PAGES } from '../app/utils/tab-pages'

// Телефон: меню профиля и фокус под шапкой.
test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

/** Пункты меню профиля: подписи и адреса по порядку. */
function menuItems(menu: ReturnType<Page['locator']>) {
  return menu.locator('[role="menuitem"]').evaluateAll(els => els.map(e => `${e.textContent?.trim()} ${e.getAttribute('href') ?? ''}`.trim()))
}

test('шторка профиля: те же пункты, что в меню на ПК, без «Настроить экран»; /screen - 404', async ({ page, guard }) => {
  guard.allow(404, /\/screen$/, 'GET')
  guard.allow(404, /\/api\/me\/layout$/, 'GET')
  await openHome(page)
  const desktop = await menuItems(page.locator('.wrap>header .pop.menu'))
  expect(desktop, 'меню профиля на ПК').toEqual(['Мой профиль /settings', 'Выйти'])
  await page.getByRole('button', { name: /^Меню @/ }).tap()
  const sheet = page.locator('.bsh.on')
  await expect(sheet).toBeVisible()
  expect(await menuItems(sheet), 'пункты шторки = пункты меню ПК').toEqual(desktop)
  expect((await page.goto('/screen'))?.status()).toBe(404)
  await expect(page.locator('.errp').getByText('404', { exact: true })).toBeVisible()
  expect((await page.request.get('/api/me/layout')).status()).toBe(404)
})

/** Где элемент в фокусе против шапки и таб-бара, после прокрутки к нему. */
function focusState(page: Page) {
  return page.evaluate(() => new Promise<{ inMain: boolean, top: number, bottom: number, head: number, bar: number, y: number, field: boolean, name: string }>((r) => {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const a = document.activeElement as HTMLElement
      const box = a.getBoundingClientRect()
      r({
        inMain: !!a.closest('main.tab.on'),
        top: box.top,
        bottom: box.bottom,
        head: document.querySelector('.wrap>header')!.getBoundingClientRect().bottom,
        bar: document.querySelector('.mbar nav')!.getBoundingClientRect().top,
        y: scrollY,
        // многострочное поле браузер прокручивает к каретке, а не целиком
        field: a.tagName === 'TEXTAREA',
        name: `${a.tagName.toLowerCase()} «${(a.textContent || a.getAttribute('aria-label') || '').trim().slice(0, 24)}»`,
      })
    }))
  }))
}

test('Tab по разделам: элемент в фокусе целиком между шапкой и таб-баром', async ({ page }) => {
  // окно пониже, чтобы прокручивался каждый раздел
  await page.setViewportSize({ width: 402, height: 560 })
  await openHome(page)
  const hidden: string[] = [], scrolled: Record<string, boolean> = {}
  const walk = async (path: string) => {
    const main = page.locator('main.tab.on')
    await expect(main).toBeVisible()
    await main.evaluate(el => Promise.all(el.getAnimations({ subtree: true })
      .filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished)))
    await page.evaluate(() => {
      (document.activeElement as HTMLElement | null)?.blur()
      scrollTo(0, 0)
    })
    // вниз до конца раздела, потом обратно вверх: прокрутка к фокусу в обе стороны
    for (const key of ['Tab', 'Shift+Tab']) {
      let entered = false
      for (let i = 0; i < 200; i++) {
        await page.keyboard.press(key)
        const s = await focusState(page)
        if (s.y > 0) scrolled[path] = true
        if (!s.inMain) {
          if (entered) break
          continue
        }
        entered = true
        if (s.top < s.head - 0.5) hidden.push(`${path} ${key}: ${s.name} верх ${Math.round(s.top)} < низа шапки ${Math.round(s.head)}`)
        if ((s.field ? s.top + 40 : s.bottom) > s.bar + 0.5) hidden.push(`${path} ${key}: ${s.name} (высота ${Math.round(s.bottom - s.top)}) низ ${Math.round(s.bottom)} > верха таб-бара ${Math.round(s.bar)}`)
      }
    }
  }
  // Клиенты с 20+ строками, как на стенде
  const ids: number[] = []
  try {
    for (let i = 0; i < 22; i++) {
      const r = await page.request.post('/api/clients', { data: { name: uniq('tabfocus'), deviceLimit: null } })
      expect(r.status()).toBe(200)
      ids.push((await r.json() as { id: number }).id)
    }
    for (const { path, label } of TAB_PAGES) {
      await page.locator('.mbar nav').getByRole('link', { name: label }).tap()
      await expect(page).toHaveURL(u => u.pathname === path)
      await walk(path)
    }
    expect(scrolled['/clients'], 'Клиенты прокручивались').toBe(true)
    expect(hidden, 'фокус под шапкой или таб-баром').toEqual([])
  }
  finally {
    for (const id of ids) await page.request.delete(`/api/clients/${id}`)
  }
})
