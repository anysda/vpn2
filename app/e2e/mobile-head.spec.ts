import type { Page } from '@playwright/test'
import { expect, test } from './support/fixtures'
import { openHome } from './support/clients'
import { TAB_PAGES } from '../app/utils/tab-pages'

// Заголовок раздела на телефоне переехал в шапку: .head, в котором остался
// только он, не занимает места. Правило общее, не копия в каждом разделе.
test.use({ viewport: { width: 402, height: 874 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

/** Верх первого видимого блока раздела. */
function firstBlockTop(page: Page) {
  return page.locator('main.tab.on').evaluate(async (main) => {
    // Раздел и его блоки въезжают анимацией: мерить после неё (бесконечные пульсы не ждать).
    await Promise.all(main.getAnimations({ subtree: true })
      .filter(a => a.effect?.getTiming().iterations !== Infinity)
      .map(a => a.finished))
    // Обёртка с display:contents своего блока не рисует: смотреть её детей.
    const blocks = (el: Element): Element[] => [...el.children]
      .flatMap(c => getComputedStyle(c).display === 'contents' ? blocks(c) : [c])
    const el = blocks(main).find(c => c.getBoundingClientRect().height > 0)
    return el ? Math.round(el.getBoundingClientRect().top + window.scrollY) : -1
  })
}

test('первый блок каждого раздела на одной высоте под шапкой, пустой .head не держит отступ', async ({ page }) => {
  await openHome(page)
  const bar = page.locator('.mbar nav')
  const tops: Record<string, number> = {}
  for (const { path, label } of TAB_PAGES) {
    await bar.getByRole('link', { name: label }).tap()
    await expect(page).toHaveURL(u => u.pathname === path)
    await expect(page.locator('main.tab.on')).toBeVisible()
    tops[path] = await firstBlockTop(page)

    // Шапка раздела только с заголовком, без своего правила раздела.
    const extra = await page.locator('main.tab.on').evaluate((main) => {
      const head = document.createElement('div')
      head.className = 'head'
      head.innerHTML = '<div><h1>Заголовок</h1><p>Подзаголовок</p></div>'
      main.prepend(head)
      return { height: head.getBoundingClientRect().height, display: getComputedStyle(head).display }
    })
    expect(extra, `${path}: .head только с заголовком`).toEqual({ height: 0, display: 'none' })
    expect(await firstBlockTop(page), `${path}: первый блок после пустой .head`).toBe(tops[path])
    await page.locator('main.tab.on > .head:first-child').evaluate(el => el.remove())
  }
  // Отступ контента под фиксированной шапкой задаёт .wrap, лишнего сверху нет.
  const want = await page.locator('.wrap').evaluate(el => Math.round(Number.parseFloat(getComputedStyle(el).paddingTop)))
  expect(tops, 'первый блок раздела').toEqual(Object.fromEntries(TAB_PAGES.map(t => [t.path, want])))
})
