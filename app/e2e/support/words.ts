/**
 * Разрывы строк посреди слова и вылезания вбок, по строкам текста элемента; для evaluate.
 * Строку даёт положение каждой буквы (Range по символу). Разрыв посреди слова допустим, только
 * если слово целиком шире строки и разрыв стоит в точке переноса по словарю, где браузер
 * рисует дефис: hyphens:auto у текста, а точки - разрывы того же слова в блоке нулевой ширины
 * (там браузер переносит в каждой точке).
 */
export function wordBreaks(el: Element) {
  // строка - ширина самого блока, у строчного элемента - его родителя
  const box = (getComputedStyle(el).display === 'inline' ? el.parentElement! : el).getBoundingClientRect()
  const tops = (node: Node) => {
    const out: { c: string, top: number, right: number }[] = []
    const walk = document.createTreeWalker(node, NodeFilter.SHOW_TEXT)
    for (let t = walk.nextNode() as Text | null; t; t = walk.nextNode() as Text | null) {
      for (let i = 0; i < t.length; i++) {
        const r = document.createRange()
        r.setStart(t, i)
        r.setEnd(t, i + 1)
        const rect = r.getBoundingClientRect()
        out.push({ c: t.data[i]!, top: Math.round(rect.top), right: rect.right })
      }
    }
    return out
  }
  // слово рядом с текстом: шрифт и язык те же
  const probe = (at: Element, word: string, css: string, fn: (m: HTMLElement) => number[]) => {
    const m = document.createElement('span')
    m.style.cssText = `position:absolute;visibility:hidden;overflow-wrap:normal;word-break:normal;${css}`
    m.textContent = word
    at.append(m)
    const res = fn(m)
    m.remove()
    return res
  }
  const owner: Element[] = []
  const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  for (let t = walk.nextNode() as Text | null; t; t = walk.nextNode() as Text | null) owner.push(...Array.from({ length: t.length }, () => t.parentElement!))
  const chars = tops(el)
  const text = chars.map(x => x.c).join('')
  const bad: string[] = []
  chars.forEach(({ c, top, right }, i) => {
    const prev = chars[i - 1]
    if (prev && top > prev.top && !/\s/.test(c) && !/[\s\-‐–—]/.test(prev.c)) {
      const from = text.lastIndexOf(' ', i) + 1
      const word = text.slice(from, (text.indexOf(' ', i) + 1 || text.length + 1) - 1)
      const at = owner[i]!
      const points = probe(at, word, 'display:block;width:0;hyphens:auto', (m) => {
        const t = tops(m)
        return t.flatMap((x, k) => k && x.top > t[k - 1]!.top ? [k] : [])
      })
      const [width] = probe(at, word, 'white-space:nowrap', m => [m.getBoundingClientRect().width])
      const cut = `«${text.slice(0, i)}|${text.slice(i)}»`
      if (getComputedStyle(at).hyphens !== 'auto' || !points.includes(i - from)) bad.push(`разрыв без дефиса ${cut}`)
      else if (width! <= box.width) bad.push(`«${word}» помещается в строку, а перенесено ${cut}`)
    }
    if (!/\s/.test(c) && right > box.right + 0.5) bad.push(`«${c}» за краем на ${Math.round(right - box.right)}px`)
  })
  return bad
}
