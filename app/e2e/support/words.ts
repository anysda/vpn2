/**
 * Разрывы строк посреди слова и вылезания вбок, по строкам текста элемента; для evaluate.
 * Строку даёт положение каждой буквы (Range по символу). Разрыв посреди слова допустим, только
 * если браузер нарисовал на нём дефис (hyphens:auto по словарю lang или мягкий дефис) и слово
 * целиком шире строки.
 *
 * Дефис переноса не символ текста: Chromium рисует его отдельным куском в конце строки и отдаёт
 * его прямоугольник в getClientRects у Range обеих букв вокруг разрыва. Поэтому строка буквы -
 * по её собственному прямоугольнику, а не по getBoundingClientRect (тот тянет букву после
 * разрыва на строку дефиса), а дефис на разрыве - прямоугольник буквы после разрыва на прежней
 * строке. Граница: так раскладывает текст Chromium (на нём идут тесты); браузер, который не
 * отдаёт дефис в прямоугольниках Range, получит здесь «разрыв без дефиса» на каждом переносе.
 */
export function wordBreaks(el: Element) {
  // строка - ширина самого блока, у строчного элемента - его родителя
  const box = (getComputedStyle(el).display === 'inline' ? el.parentElement! : el).getBoundingClientRect()
  const chars: { c: string, top: number, right: number, hyphen: boolean, at: Element }[] = []
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
        chars.push({ c: t.data[i]!, top: chars.at(-1)?.top ?? 0, right: -Infinity, hyphen: false, at: t.parentElement! })
        continue
      }
      // свой прямоугольник - на нижней строке, выше него может стоять только дефис прежней строки
      const top = Math.max(...rects.map(q => Math.round(q.top)))
      const own = rects.filter(q => Math.round(q.top) === top)
      chars.push({
        c: t.data[i]!,
        top,
        right: Math.max(...own.map(q => q.right)),
        hyphen: own.length < rects.length,
        at: t.parentElement!,
      })
    }
  }
  const text = chars.map(x => x.c).join('')
  const bad: string[] = []
  chars.forEach(({ c, top, right, hyphen, at }, i) => {
    const prev = chars[i - 1]
    if (prev && top > prev.top && !/\s/.test(c) && !/[\s\-‐–—]/.test(prev.c)) {
      const from = text.lastIndexOf(' ', i) + 1
      const word = text.slice(from, (text.indexOf(' ', i) + 1 || text.length + 1) - 1)
      // ширина слова одной строкой рядом с текстом: шрифт тот же
      const m = document.createElement('span')
      m.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap'
      m.textContent = word
      at.append(m)
      const width = m.getBoundingClientRect().width
      m.remove()
      const cut = `«${text.slice(0, i)}|${text.slice(i)}»`
      if (!hyphen) bad.push(`разрыв без дефиса ${cut}`)
      else if (width <= box.width) bad.push(`«${word}» помещается в строку, а перенесено ${cut}`)
    }
    if (!/\s/.test(c) && right > box.right + 0.5) bad.push(`«${c}» за краем на ${Math.round(right - box.right)}px`)
  })
  return bad
}
