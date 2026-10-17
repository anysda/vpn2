import type { Page } from '@playwright/test'
import { expect } from './fixtures'

/**
 * Намеренные ленты прокрутки вбок: их содержимое шире самой ленты по замыслу.
 * Селектор ставится на сам прокручиваемый блок.
 */
const SCROLL_STRIPS = [
  '.dns .dseg', // подразделы DNS на телефоне
  '.dns .tbar .seg', // фильтр журнала DNS на телефоне
]

/**
 * Ничего не вылезает вбок: ни за окно (ширина документа не больше окна), ни за
 * край ближайшего обрезающего или прокручиваемого предка — такой элемент
 * молча обрезан `overflow: hidden` или уехал в прокрутку внутри блока.
 * Мерим после конечных анимаций. `allow` — селекторы лент прокрутки сверх SCROLL_STRIPS.
 */
export async function noHorizontalOverflow(page: Page, where: string, allow: string[] = []) {
  const m = await page.evaluate(async (strips) => {
    // Въезд экрана (сдвиг вбок на время анимации) — не вылезание: мерить, когда встало.
    await Promise.all(document.getAnimations()
      .filter(a => a.effect?.getTiming().iterations !== Infinity)
      .map(a => a.finished.catch(() => {})))
    const vw = document.documentElement.clientWidth
    const name = (el: Element) => `${el.tagName.toLowerCase()}.${[...el.classList].slice(0, 4).join('.')}`
    const shown = (el: Element) => {
      const r = el.getBoundingClientRect()
      const s = getComputedStyle(el)
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.position !== 'fixed'
    }
    // Внутри svg свои правила обрезки (viewBox): сам svg проверяем, его детей нет.
    const els = [...document.querySelectorAll('body *')]
      .filter(el => el instanceof HTMLElement || el instanceof SVGSVGElement)
      .filter(shown)
    const wide = els
      .filter(el => el.getBoundingClientRect().right > vw + 1)
      .slice(0, 5)
      .map(el => `${name(el)} right=${Math.round(el.getBoundingClientRect().right)}`)
    const clipped: string[] = []
    for (const el of els) {
      let box = el.parentElement
      while (box && box !== document.body && getComputedStyle(box).overflowX === 'visible') box = box.parentElement
      if (!box || box === document.body) continue
      const clip = box
      // Многоточие у текста — обрезка по замыслу.
      if (getComputedStyle(clip).textOverflow === 'ellipsis') continue
      if (strips.some(s => clip.matches(s))) continue
      const edge = clip.getBoundingClientRect().left + clip.clientLeft + clip.clientWidth
      const right = el.getBoundingClientRect().right
      if (right > edge + 1) clipped.push(`${name(el)} right=${Math.round(right)} > ${name(clip)} ${Math.round(edge)}`)
      if (clipped.length === 5) break
    }
    return { vw, sw: document.documentElement.scrollWidth, wide, clipped }
  }, [...SCROLL_STRIPS, ...allow])
  expect(m.sw, `${where}: scrollWidth ${m.sw} > ${m.vw}; вылезают: ${m.wide.join('; ')}`).toBeLessThanOrEqual(m.vw)
  expect(m.clipped, `${where}: вылезают за край блока`).toEqual([])
}

/**
 * Ничего не вылезает вниз за свой блок: каждый элемент внутри `root` целиком
 * выше нижнего поля родителя. Ловит блоки с жёсткой высотой, в
 * которых перенесённый текст вылезает или молча обрезан `overflow: hidden`.
 * Прокручиваемые блоки и вынутые из потока элементы не проверяются. Мерим после конечных анимаций.
 */
export async function noVerticalOverflow(page: Page, root: string, where: string) {
  const out = await page.locator(root).first().evaluate(async (top) => {
    await Promise.all(document.getAnimations()
      .filter(a => a.effect?.getTiming().iterations !== Infinity)
      .map(a => a.finished.catch(() => {})))
    const name = (el: Element) => `${el.tagName.toLowerCase()}.${[...el.classList].slice(0, 4).join('.')}`
    const res: string[] = []
    for (const el of top.querySelectorAll('*')) {
      if (!(el instanceof HTMLElement || el instanceof SVGSVGElement)) continue
      const p = el.parentElement!
      const s = getComputedStyle(el), ps = getComputedStyle(p)
      if (s.position === 'absolute' || s.position === 'fixed' || ps.overflowY === 'auto' || ps.overflowY === 'scroll') continue
      const r = el.getBoundingClientRect()
      if (!r.width || !r.height) continue
      // край содержимого: при высоте по содержимому ребёнок до него и доходит, дальше — только когда высота зажата
      const edge = p.getBoundingClientRect().top + p.clientTop + p.clientHeight - Number.parseFloat(ps.paddingBottom)
      if (r.bottom > edge + 1) res.push(`${name(el)} «${(el.textContent ?? '').trim().slice(0, 30)}» низ ${Math.round(r.bottom)} > ${name(p)} ${Math.round(edge)}`)
    }
    return res
  })
  expect(out, `${where}: вылезают вниз за свой блок`).toEqual([])
}
