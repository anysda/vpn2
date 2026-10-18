/**
 * Разрывы строк посреди слова и вылезания вбок, по строкам текста элемента; для evaluate.
 * Слово не делится никогда: перенос по слогам с дефисом браузера (hyphens:auto, мягкий дефис)
 * такой же разрыв, как без дефиса. Строка может начаться только после пробела или дефиса,
 * тире, косой черты в самом тексте.
 *
 * Строку даёт положение каждой буквы (Range по символу). Дефис переноса не символ текста:
 * Chromium рисует его отдельным куском в конце строки и отдаёт его прямоугольник в
 * getClientRects у Range обеих букв вокруг разрыва. Поэтому строка буквы - по её нижнему
 * прямоугольнику, а не по getBoundingClientRect (тот тянет букву после разрыва на строку
 * дефиса). Граница: так раскладывает текст Chromium (на нём идут тесты).
 */
export function wordBreaks(el: Element) {
  // строка - ширина самого блока, у строчного элемента - его родителя
  const box = (getComputedStyle(el).display === 'inline' ? el.parentElement! : el).getBoundingClientRect()
  const chars: { c: string, top: number, right: number }[] = []
  const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  for (let t = walk.nextNode() as Text | null; t; t = walk.nextNode() as Text | null) {
    for (let i = 0; i < t.length; i++) {
      const r = document.createRange()
      r.setStart(t, i)
      r.setEnd(t, i + 1)
      const all = Array.from(r.getClientRects())
      const wide = all.filter(q => q.width > 0)
      // пробел на конце строки бывает нулевой ширины, а то и без прямоугольника
      const rects = wide.length ? wide : all
      if (!rects.length) {
        chars.push({ c: t.data[i]!, top: chars.at(-1)?.top ?? 0, right: -Infinity })
        continue
      }
      // свой прямоугольник - на нижней строке, выше него может стоять только дефис прежней строки
      const top = Math.max(...rects.map(q => Math.round(q.top)))
      const own = rects.filter(q => Math.round(q.top) === top)
      chars.push({ c: t.data[i]!, top, right: Math.max(...own.map(q => q.right)) })
    }
  }
  const text = chars.map(x => x.c).join('')
  const bad: string[] = []
  chars.forEach(({ c, top, right }, i) => {
    const prev = chars[i - 1]
    if (prev && top > prev.top && !/\s/.test(c) && !/[\s\-‐–—/]/.test(prev.c)) {
      bad.push(`разрыв посреди слова «${text.slice(0, i)}|${text.slice(i)}»`)
    }
    if (!/\s/.test(c) && right > box.right + 0.5) bad.push(`«${c}» за краем на ${Math.round(right - box.right)}px`)
  })
  return bad
}
