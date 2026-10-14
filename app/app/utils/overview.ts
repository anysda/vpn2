import { inject, type InjectionKey } from 'vue'

// Графики и движение обзора, перенесены из макета без изменений.

const EO = 'cubic-bezier(.23,1,.32,1)'
export const EIO = 'cubic-bezier(.77,0,.175,1)'
export const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches

export function anim(e: Element | null | undefined, k: Keyframe[], o: KeyframeAnimationOptions) {
  if (reduced() || !e) return
  e.animate(k, { easing: EO, fill: 'backwards', ...o })
}

type Pt = [number, number]

/** Catmull-Rom через кубические Безье. */
export function smooth(pts: Pt[]): string {
  let d = `M${pts[0]![0]},${pts[0]![1]}`
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i]!, p1 = pts[i]!, p2 = pts[i + 1]!, p3 = pts[i + 2] || p2
    d += ` C${p1[0] + (p2[0] - p0[0]) / 6},${p1[1] + (p2[1] - p0[1]) / 6} ${p2[0] - (p3[0] - p1[0]) / 6},${p2[1] - (p3[1] - p1[1]) / 6} ${p2[0]},${p2[1]}`
  }
  return d
}

/** Точки линии: значения растянуты между min и max с отступом pad сверху и снизу. */
export function linePoints(data: number[], W: number, H: number, pad: number): Pt[] {
  const mx = Math.max(...data), mn = Math.min(...data)
  return data.map((v, i) => [i * (W / (data.length - 1)), pad + (H - 2 * pad) * (1 - (v - mn) / (mx - mn || 1))])
}

/** Окно из `len` соседних точек с наибольшей суммой — подсвеченная полоса графика. */
export function peakBand(data: number[], len = 4): [number, number] | null {
  if (data.length < len) return null
  let best = 0, sum = -Infinity
  for (let i = 0; i + len <= data.length; i++) {
    const s = data.slice(i, i + len).reduce((a, b) => a + b, 0)
    if (s >= sum) { sum = s; best = i }
  }
  return [best, best + len - 1]
}

// счёт с нуля: 214 → «214», 9,1% → «9,1%», 2 283 → «2 283»
export function count(e: Element | null | undefined, dur = 900, delay = 0) {
  if (reduced() || !e) return
  const t = [...e.childNodes].find((n): n is Text => n.nodeType === 3 && /\d/.test((n as Text).data))
  if (!t) return
  const src = t.data, m = src.match(/\d[\d\s]*(?:,\d+)?/)
  if (!m) return
  const dec = (m[0].split(',')[1] || '').length, v = +m[0].replace(/\s/g, '').replace(',', '.')
  const fmt = (x: number) => {
    let s = x.toFixed(dec).replace('.', ',')
    if (/\s/.test(m[0])) s = s.replace(/\B(?=(\d{3})+(?!\d))/g, ' ')
    return src.replace(m[0], s)
  }
  const t0 = performance.now() + delay
  t.data = fmt(0)
  const f = (now: number) => {
    const k = Math.min(1, Math.max(0, (now - t0) / dur)), q = 1 - Math.pow(1 - k, 4)
    t.data = fmt(v * q)
    if (k < 1) requestAnimationFrame(f)
    else t.data = src
  }
  requestAnimationFrame(f)
}

// линия рисуется слева направо, заливка проявляется следом, точки загораются, когда линия до них дошла
export function drawSvg(svg: SVGSVGElement | null | undefined, delay = 0, dur = 1100) {
  if (reduced() || !svg) return
  const P = [...svg.querySelectorAll('path')], ln = P.find(p => p.getAttribute('fill') === 'none')
  if (!ln) return
  const L = ln.getTotalLength()
  ln.animate([{ strokeDasharray: `${L} ${L}`, strokeDashoffset: L }, { strokeDasharray: `${L} ${L}`, strokeDashoffset: 0 }], { duration: dur, delay, easing: EIO, fill: 'backwards' })
  P.filter(p => p !== ln).forEach(p => anim(p, [{ opacity: 0 }, { opacity: 1 }], { duration: 600, delay: delay + dur * 0.55 }))
  anim(svg.querySelector('rect'), [{ opacity: 0 }, { opacity: 1 }], { duration: 500, delay: delay + dur * 0.7 })
  const r = svg.getBoundingClientRect()
  svg.parentElement?.querySelectorAll<HTMLElement>(':scope>.pt').forEach((d) => {
    const x = (d.getBoundingClientRect().left - r.left) / r.width
    anim(d, [{ opacity: 0, transform: 'scale(.4)' }, { opacity: 1, transform: 'none' }], { duration: 320, delay: delay + dur * Math.min(1, x * 1.05) })
  })
}

// кольцо закручивается по часовой: каждый сегмент тянется, когда до него дошла очередь
export function sweep(svg: SVGSVGElement | null | undefined, delay = 0, dur = 1000) {
  if (reduced() || !svg) return
  const S = [...svg.querySelectorAll('circle[stroke-dasharray]')]
  if (!S.length) return
  const C = 2 * Math.PI * +S[0]!.getAttribute('r')!
  S.forEach((c) => {
    const [len, rest] = c.getAttribute('stroke-dasharray')!.split(' ').map(Number)
    const start = (-(c.getAttribute('stroke-dashoffset') || 0)) / C, part = len! / C
    c.animate([{ strokeDasharray: `0 ${C}` }, { strokeDasharray: `${len} ${rest}` }],
      { duration: Math.max(120, dur * part), delay: delay + dur * start, easing: 'cubic-bezier(.33,1,.68,1)', fill: 'backwards' })
  })
}

// перекат цифр: меняются только те разряды, что изменились; рост катится вверх, спад вниз
export function roll(w: HTMLElement, s: string) {
  const old = w.textContent ?? ''
  if (old === s) return
  if (reduced() || old.length !== s.length) {
    w.textContent = s
    return
  }
  const up = parseFloat(s.replace(/\s/g, '').replace(',', '.')) >= parseFloat(old.replace(/\s/g, '').replace(',', '.'))
  w.textContent = ''
  ;[...s].forEach((ch, i) => {
    if (ch === old[i]) {
      w.append(ch)
      return
    }
    const d = document.createElement('x-d')
    const n = document.createElement('x-n'), o = document.createElement('x-o')
    n.textContent = ch
    o.textContent = old[i]!
    d.append(n, o)
    w.append(d)
    const a = i * 30, dir = up ? 1 : -1
    n.animate([{ transform: `translateY(${70 * dir}%)`, opacity: 0 }, { transform: 'none', opacity: 1 }], { duration: 460, delay: a, easing: EO, fill: 'backwards' })
    o.animate([{ transform: 'none', opacity: 1 }, { transform: `translateY(${-70 * dir}%)`, opacity: 0 }], { duration: 360, delay: a, easing: EO, fill: 'forwards' }).onfinish = () => o.remove()
  })
}

/**
 * Первая сцена обзора: сеть «просыпается» один раз за открытие панели, дальше
 * обзор приходит обычным каскадом карточек. Задержки сцены отсчитываются от
 * открытия страницы: данные, пришедшие позже, догоняют свою очередь.
 */
export interface Wake { on: boolean, t0: number }
export const WAKE: InjectionKey<Wake> = Symbol('overview-wake')

export function useWake() {
  const w = inject(WAKE, { on: false, t0: 0 })
  return {
    get on() { return w.on },
    at: (ms: number) => Math.max(0, ms - (performance.now() - w.t0)),
  }
}

const nf = (d: number) => new Intl.NumberFormat('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d })
/** Число по-русски: запятая и пробелы между разрядами, как в макете. */
export function ru(v: number, digits = 0): string {
  return nf(digits).format(v).replace(/\u00A0|\u202F/g, ' ')
}
