import type { Page } from '@playwright/test'
import { expect, NODES, test, uniq } from './support/fixtures'
import { openHome } from './support/clients'
import { orderBreaks } from './support/order'
import { TAB_PAGES } from '../app/utils/tab-pages'

// Телефон и «Настроить экран»: пункт меню, порядок блоков Обзора, фокус под шапкой.
test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

const defaults = { left: ['traffic', 'nodes', 'activity'], right: [], hidden: [] }
const headBottom = (page: Page) => page.locator('.wrap>header').evaluate(el => el.getBoundingClientRect().bottom)

/** Пункты меню профиля: подписи и адреса по порядку. */
function menuItems(menu: ReturnType<Page['locator']>) {
  return menu.locator('[role="menuitem"]').evaluateAll(els => els.map(e => `${e.textContent?.trim()} ${e.getAttribute('href') ?? ''}`.trim()))
}

test('шторка профиля: те же пункты, что в меню на ПК, «Настроить экран» открывает /screen', async ({ page }) => {
  await openHome(page)
  const desktop = await menuItems(page.locator('.wrap>header .pop.menu'))
  await page.getByRole('button', { name: /^Меню @/ }).tap()
  const sheet = page.locator('.bsh.on')
  await expect(sheet).toBeVisible()
  expect(await menuItems(sheet), 'пункты шторки = пункты меню ПК').toEqual(desktop)
  await sheet.getByRole('menuitem', { name: 'Настроить экран' }).tap()
  await expect(page).toHaveURL(u => u.pathname === '/screen')
  await expect(sheet).toHaveCount(0)
  const main = page.locator('main.tab.on.scr')
  await expect(main).toBeVisible()
  // кнопки страницы видны целиком: ниже шапки, в окне
  const bottom = await headBottom(page)
  for (const c of await main.locator('.head').locator('button, a').all()) {
    const r = (await c.boundingBox())!
    expect(r.y, `${await c.textContent()}: под шапкой`).toBeGreaterThanOrEqual(bottom)
    expect(r.x + r.width, `${await c.textContent()}: в окне`).toBeLessThanOrEqual(402)
  }
})

test.describe('порядок блоков Обзора из «Настроить экран»', () => {
  test.afterEach(({ page }) => page.request.put('/api/me/layout', { data: defaults }).then(() => undefined))

  // каждая перестановка и скрытие: блоки на телефоне в порядке /screen, разметка — как на экране
  const layouts: { on: string[], hidden: string[] }[] = [
    { on: ['activity', 'traffic', 'nodes'], hidden: [] },
    { on: ['activity', 'nodes', 'traffic'], hidden: [] },
    { on: ['nodes', 'traffic', 'activity'], hidden: [] },
    { on: ['nodes', 'activity', 'traffic'], hidden: [] },
    { on: ['traffic', 'activity', 'nodes'], hidden: [] },
    { on: ['traffic', 'nodes', 'activity'], hidden: [] },
    { on: ['activity', 'nodes'], hidden: ['traffic'] },
    { on: ['traffic'], hidden: ['nodes', 'activity'] },
  ]
  // первый блок модуля на экране
  const lead: Record<string, string> = { traffic: '.ov .ev', nodes: '.ov-nodes', activity: '.ov-activity' }

  for (const { on, hidden } of layouts) {
    test(`${on.join(', ')}${hidden.length ? ` (скрыто ${hidden.join(', ')})` : ''}`, async ({ page }) => {
      await openHome(page)
      const saved = await page.request.put('/api/me/layout', { data: { left: on, right: [], hidden } })
      expect(saved.ok()).toBe(true)
      await page.reload()
      if (on.includes('nodes')) for (const tag of NODES) await expect(page.locator(`.ov-nodes .node[data-tag="${tag}"]`)).toBeVisible()
      const main = page.locator('main.tab.on')
      await expect(main.locator('[data-blk]').first()).toBeVisible()
      expect(await main.locator('[data-blk]').evaluateAll(els => els.map(e => e.getAttribute('data-blk'))), 'модули в разметке').toEqual(on)
      for (const id of hidden) await expect(page.locator(lead[id]!)).toHaveCount(0)
      const tops = []
      for (const id of on) tops.push(Math.round((await page.locator(lead[id]!).first().boundingBox())!.y))
      expect(tops, `${on.join(', ')}: сверху вниз`).toEqual([...tops].sort((a, b) => a - b))
      expect(await orderBreaks(main, 'blocks'), 'блоки по разметке против экрана').toEqual([])
    })
  }
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
    await page.goto('/screen')
    await walk('/screen')
    expect(scrolled['/clients'], 'Клиенты прокручивались').toBe(true)
    expect(hidden, 'фокус под шапкой или таб-баром').toEqual([])
  }
  finally {
    for (const id of ids) await page.request.delete(`/api/clients/${id}`)
  }
})
