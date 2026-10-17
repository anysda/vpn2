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
 * `allow` — селекторы лент прокрутки сверх SCROLL_STRIPS.
 */
export async function noHorizontalOverflow(page: Page, where: string, allow: string[] = []) {
  const m = await page.evaluate((strips) => {
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
