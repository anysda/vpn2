import type { Page } from '@playwright/test'
import { expect } from './fixtures'

/** Ширина документа не больше окна: ничего не вылезает вбок. */
export async function noHorizontalOverflow(page: Page, where: string) {
  const m = await page.evaluate(() => {
    const vw = document.documentElement.clientWidth
    const wide = [...document.querySelectorAll<HTMLElement>('body *')]
      .filter((el) => {
        const r = el.getBoundingClientRect()
        return r.width > 0 && r.right > vw + 1 && getComputedStyle(el).position !== 'fixed'
      })
      .slice(0, 5)
      .map(el => `${el.tagName.toLowerCase()}.${[...el.classList].slice(0, 4).join('.')} right=${Math.round(el.getBoundingClientRect().right)}`)
    return { vw, sw: document.documentElement.scrollWidth, wide }
  })
  expect(m.sw, `${where}: scrollWidth ${m.sw} > ${m.vw}; вылезают: ${m.wide.join('; ')}`).toBeLessThanOrEqual(m.vw)
}
