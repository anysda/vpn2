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
  const tops: Record<string, number> = {}, pads: Record<string, number> = {}
  for (const { path, label } of TAB_PAGES) {
    await bar.getByRole('link', { name: label }).tap()
    await expect(page).toHaveURL(u => u.pathname === path)
    await expect(page.locator('main.tab.on')).toBeVisible()
    tops[path] = await firstBlockTop(page)
    pads[path] = await page.locator('.wrap').evaluate(el => Math.round(Number.parseFloat(getComputedStyle(el).paddingTop)))

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
  expect(tops, 'первый блок раздела').toEqual(Object.fromEntries(TAB_PAGES.map(t => [t.path, pads[t.path]])))
})

/** Низ фиксированной шапки. */
const headBottom = (page: Page) => page.locator('.wrap>header').evaluate(el => el.getBoundingClientRect().bottom)

test('первый блок на 4px ниже шапки на каждом разделе и при выросшей шапке тоже', async ({ page }) => {
  await openHome(page)
  const got: Record<string, number> = {}, want: Record<string, number> = {}
  const check = async (key: string) => {
    want[key] = Math.round(await headBottom(page) + 4)
    got[key] = await firstBlockTop(page)
  }
  for (const { path, label } of TAB_PAGES) {
    await page.locator('.mbar nav').getByRole('link', { name: label }).tap()
    await expect(page).toHaveURL(u => u.pathname === path)
    await expect(page.locator('main.tab.on')).toBeVisible()
    await check(path)
  }
  // шапка любой высоты: отступ идёт за ней
  await page.locator('.wrap>header').evaluate((el) => { el.style.paddingBottom = '40px' })
  await expect.poll(() => headBottom(page)).toBeGreaterThan(90)
  // новая высота шапки доходит до отступа к следующему кадру
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))))
  await check('шапка выше')
  expect(got, 'верх первого блока = низ шапки + 4').toEqual(want)
})
