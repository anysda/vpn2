import type { Page } from '@playwright/test'

export type Rect = { x: number, y: number, width: number, height: number }

/**
 * Прямоугольники первых совпавших элементов по селекторам — за один кадр.
 * Два boundingBox() подряд могут попасть в разные кадры анимации появления
 * и разъехаться на её сдвиг; здесь всё меряется одним getBoundingClientRect()
 * в одной задаче. Нет элемента или он скрыт (прямоугольник 0×0) — ошибка
 * с именем селектора, как null у boundingBox().
 */
export async function rects<const S extends string[]>(page: Page, ...selectors: S): Promise<{ [K in keyof S]: Rect }> {
  const got = await page.evaluate(sels => sels.map((s) => {
    const el = document.querySelector(s)
    if (!el) return 'нет элемента'
    const r = el.getBoundingClientRect()
    if (!r.width && !r.height) return 'элемент скрыт'
    return { x: r.x, y: r.y, width: r.width, height: r.height }
  }), selectors)
  const bad = selectors.flatMap((s, i) => typeof got[i] === 'string' ? [`${got[i]}: ${s}`] : [])
  if (bad.length) throw new Error(bad.join('; '))
  return got as { [K in keyof S]: Rect }
}
