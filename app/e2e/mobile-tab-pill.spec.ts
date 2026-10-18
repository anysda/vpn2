import type { Page } from '@playwright/test'
import { expect, test } from './support/fixtures'
import { openHome } from './support/clients'
import { wordBreaks } from './support/words'
import { TAB_PAGES } from '../app/utils/tab-pages'

// Таб-бар телефона: пилюля активной вкладки облегает слово с равными отступами,
// видимые промежутки между словами равны, бар целиком в ширине экрана.
// Мерим геометрию элементов (Range по буквам слова, прямоугольники пилюли и вкладок),
// не ширину шрифта: её headless Chromium округляет.

/** Слово каждой вкладки (без пробелов вокруг), пилюля, вкладки и сам бар. */
function geometry(nav: Element) {
  const word = (a: Element) => {
    const t = Array.from(a.childNodes).find(n => n.nodeType === Node.TEXT_NODE && n.textContent!.trim()) as Text
    const s = t.data.search(/\S/), e = t.data.trimEnd().length
    const r = document.createRange()
    r.setStart(t, s)
    r.setEnd(t, e)
    const q = r.getBoundingClientRect()
    return { left: q.left, right: q.right }
  }
  const links = Array.from(nav.querySelectorAll<HTMLElement>(':scope>a'))
  const g = nav.querySelector('.glide')!.getBoundingClientRect()
  const n = nav.getBoundingClientRect()
  return {
    words: links.map(word),
    links: links.map(a => ({ left: a.getBoundingClientRect().left, right: a.getBoundingClientRect().right, height: a.getBoundingClientRect().height })),
    on: links.findIndex(a => a.classList.contains('on')),
    pill: { left: g.left, right: g.right, width: g.width },
    nav: { left: n.left, right: n.right, scroll: nav.scrollWidth - nav.clientWidth },
    vw: document.documentElement.clientWidth,
  }
}

const round = (x: number) => Math.round(x * 10) / 10

async function settled(page: Page) {
  const nav = page.locator('.mbar nav')
  // пилюля доехала: стоит ровно на активной вкладке
  await expect.poll(() => nav.evaluate((el) => {
    const g = el.querySelector('.glide')!.getBoundingClientRect(), a = el.querySelector(':scope>a.on')!.getBoundingClientRect()
    return Math.abs(g.left - a.left) < 0.5 && Math.abs(g.width - a.width) < 0.5
  })).toBe(true)
  return nav.evaluate(geometry)
}

for (const width of [360, 402]) {
  test.describe(`ширина ${width}`, () => {
    test.use({ viewport: { width, height: 800 } })

    test('пилюля по слову с равными отступами, промежутки между словами равны, бар в экране', async ({ page }) => {
      await openHome(page)
      const nav = page.locator('.mbar nav')
      await expect(nav).toBeVisible()

      for (const { path, label } of TAB_PAGES) {
        await nav.getByRole('link', { name: label }).tap()
        await expect(page).toHaveURL(u => u.pathname === path)
        const m = await settled(page)
        const w = m.words[m.on]!
        const left = w.left - m.pill.left, right = m.pill.right - w.right
        expect(Math.abs(left - right), `${label}: отступ слева ${round(left)} справа ${round(right)}`).toBeLessThanOrEqual(1)
      }

      const m = await nav.evaluate(geometry)
      const gaps = m.words.slice(1).map((w, i) => w.left - m.words[i]!.right)
      expect(Math.max(...gaps) - Math.min(...gaps), `промежутки ${gaps.map(round).join(', ')}`).toBeLessThanOrEqual(1)
      // отступ слова в пилюле у всех вкладок один (±1px): пилюля «по слову», а не по ячейке
      const pads = m.words.map((w, i) => w.left - m.links[i]!.left)
      expect(Math.max(...pads) - Math.min(...pads), `отступы в вкладках ${pads.map(round).join(', ')}`).toBeLessThanOrEqual(1)

      expect(m.nav.scroll, 'бар без прокрутки').toBeLessThanOrEqual(0)
      expect(m.nav.left, 'бар не за левым краем').toBeGreaterThanOrEqual(0)
      expect(m.nav.right, 'бар не за правым краем').toBeLessThanOrEqual(m.vw)
      for (const [i, l] of m.links.entries()) {
        expect(l.height, `${TAB_PAGES[i]!.label}: высота зоны нажатия`).toBeGreaterThanOrEqual(44)
        expect(m.words[i]!.left, `${TAB_PAGES[i]!.label}: слово не обрезано слева`).toBeGreaterThanOrEqual(l.left - 0.5)
        expect(m.words[i]!.right, `${TAB_PAGES[i]!.label}: слово не обрезано справа`).toBeLessThanOrEqual(l.right + 0.5)
      }
      expect(await nav.evaluate(wordBreaks)).toEqual([])
    })

    test('пилюля едет к новой вкладке и меняет ширину', async ({ page }) => {
      await openHome(page)
      const nav = page.locator('.mbar nav')
      const from = await settled(page)
      // покадровая запись пилюли на время перехода
      await nav.evaluate((el) => {
        const g = el.querySelector('.glide')!, w = window as unknown as { pillFrames: { left: number, width: number }[] }
        w.pillFrames = []
        const t0 = performance.now()
        const tick = () => {
          const r = g.getBoundingClientRect()
          w.pillFrames.push({ left: r.left, width: r.width })
          if (performance.now() - t0 < 1500) requestAnimationFrame(tick)
        }
        requestAnimationFrame(tick)
      })
      await nav.getByRole('link', { name: 'Настройки' }).tap()
      const to = await settled(page)
      const frames = await page.evaluate(() => (window as unknown as { pillFrames: { left: number, width: number }[] }).pillFrames)
      // в пути: кадры, где пилюля уже не на «Обзоре», но ещё не на «Настройках», и ширина промежуточная
      const between = (x: number, a: number, b: number) => x > Math.min(a, b) + 1 && x < Math.max(a, b) - 1
      expect(frames.filter(f => between(f.left, from.pill.left, to.pill.left)).length, 'кадры в пути').toBeGreaterThan(2)
      expect(frames.filter(f => between(f.width, from.pill.width, to.pill.width)).length, 'ширина меняется плавно').toBeGreaterThan(2)
      expect(to.on).toBe(TAB_PAGES.findIndex(t => t.label === 'Настройки'))
      expect(to.pill.left, 'пилюля уехала вправо').toBeGreaterThan(from.pill.left)
      expect(Math.abs(to.pill.width - from.pill.width), 'ширина пилюли по слову').toBeGreaterThan(5)
    })
  })
}
