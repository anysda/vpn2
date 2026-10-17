import type { Locator } from '@playwright/test'

/**
 * Пары, где разметка и экран расходятся: элемент раньше по разметке стоит на
 * экране ниже или правее следующего за ним. Tab и скринридер идут по разметке.
 * `focusable` — фокусируемое внутри, иначе блоки (обёртки display:contents раскрыты).
 */
export function orderBreaks(root: Locator, what: 'focusable' | 'blocks') {
  return root.evaluate(async (el, what) => {
    await Promise.all(el.getAnimations({ subtree: true })
      .filter(a => a.effect?.getTiming().iterations !== Infinity)
      .map(a => a.finished))
    const blocks = (e: Element): Element[] => [...e.children]
      .flatMap(c => getComputedStyle(c).display === 'contents' ? blocks(c) : [c])
    const list = what === 'blocks'
      ? blocks(el)
      : [...el.querySelectorAll('a[href],button,input,select,textarea,[tabindex]:not([tabindex="-1"])')]
          .filter(e => !e.closest('[inert]') && !(e as HTMLButtonElement).disabled)
    const seen = list
      .map(e => ({ e, r: e.getBoundingClientRect() }))
      .filter(({ e, r }) => r.width > 0 && r.height > 0 && getComputedStyle(e).visibility === 'visible')
    const name = (e: Element) => `${e.tagName.toLowerCase()}.${[...e.classList].join('.')} «${(e.textContent ?? '').trim().slice(0, 24)}»`
    const out: string[] = []
    for (let i = 0; i < seen.length; i++) {
      for (let j = i + 1; j < seen.length; j++) {
        const a = seen[i]!.r, b = seen[j]!.r
        const above = b.bottom <= a.top + 1
        const leftInRow = b.top < a.bottom - 1 && b.bottom > a.top + 1 && b.right <= a.left + 1
        if (above || leftInRow) out.push(`${name(seen[i]!.e)} раньше ${name(seen[j]!.e)}`)
      }
    }
    return out
  }, what)
}
