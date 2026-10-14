// Движение вкладки DNS по макету: появление, лесенка карточек, смена
// подраздела через View Transitions, кривые графиков. Сама анимация — общая
// anim из clients-view: то же движение, что на экране клиентов.
import { anim } from './clients-view'

export const EO = 'cubic-bezier(.23,1,.32,1)'

export const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches

let reveal: IntersectionObserver | null | undefined

function revealer() {
  if (reveal !== undefined) return reveal
  reveal = 'IntersectionObserver' in window && !reducedMotion()
    ? new IntersectionObserver((entries) => {
        let k = 0
        entries.forEach((e) => {
          if (!e.isIntersecting) return
          reveal?.unobserve(e.target)
          e.target.classList.remove('rv')
          anim(e.target, [{ opacity: 0, transform: 'translateY(18px)' }, { opacity: 1, transform: 'none' }], { duration: 560, delay: k++ * 60 })
        })
      }, { rootMargin: '0px 0px -6% 0px' })
    : null
  return reveal
}

/** Карточки подраздела поднимаются лесенкой, те, что ниже экрана, — при прокрутке. */
export function cascade(box: HTMLElement | null | undefined) {
  if (!box || reducedMotion()) return
  const cards = [...box.querySelectorAll<HTMLElement>('.card, .card-ui')]
    .filter(c => c.offsetWidth && !c.parentElement?.closest('.card, .card-ui'))
  let k = 0
  cards.forEach((c) => {
    if (c.getBoundingClientRect().top < innerHeight) {
      anim(c, [{ opacity: 0, transform: 'translateY(22px)' }, { opacity: 1, transform: 'none' }], { duration: 520, delay: 60 + Math.min(k++, 8) * 55 })
    }
    else {
      const io = revealer()
      if (io && !c.dataset.rv) {
        c.dataset.rv = '1'
        c.classList.add('rv')
        io.observe(c)
      }
    }
  })
}

/** Смена подраздела: старый уходит, новый въезжает со стороны нажатой кнопки. */
export async function subTransition(dir: 'fwd' | 'back', change: () => Promise<void>) {
  if (reducedMotion() || !document.startViewTransition) {
    await change()
    return
  }
  const html = document.documentElement
  html.dataset.vt = 'sub'
  html.dataset.dir = dir
  const t = document.startViewTransition(change)
  await t.finished.catch(() => {}).finally(() => {
    delete html.dataset.vt
    delete html.dataset.dir
    document.querySelectorAll('.tab.on, .dpan.on').forEach(el => el.getAnimations().forEach((a) => {
      if ((a as CSSAnimation).animationName === 'tfade') a.finish()
    }))
  })
}

/** Плавная кривая через точки (Catmull-Rom в кубических Безье), как в макете. */
export function smooth(pts: Array<[number, number]>): string {
  if (!pts.length) return ''
  let d = `M${pts[0]![0]},${pts[0]![1]}`
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] ?? pts[i]!, p1 = pts[i]!, p2 = pts[i + 1]!, p3 = pts[i + 2] ?? p2
    d += ` C${p1[0] + (p2[0] - p0[0]) / 6},${p1[1] + (p2[1] - p0[1]) / 6} ${p2[0] - (p3[0] - p1[0]) / 6},${p2[1] - (p3[1] - p1[1]) / 6} ${p2[0]},${p2[1]}`
  }
  return d
}

/** Точки спарклайна в рамке W×H с отступом: минимум внизу, максимум вверху. */
export function sparkPoints(data: number[], W: number, H: number, pad: number): Array<[number, number]> {
  const v = data.length > 1 ? data : [data[0] ?? 0, data[0] ?? 0]
  const mx = Math.max(...v), mn = Math.min(...v)
  return v.map((x, i) => [i * (W / (v.length - 1)), pad + (H - 2 * pad) * (1 - (x - mn) / (mx - mn || 1))])
}

/** Число с пробелами по разрядам: 2283 → «2 283». */
export function grp(n: number): string {
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')
}
