import { parseOutbound } from '#shared/outbounds'

type Outbound = Api<'/api/routes/outbounds'>[number]

interface Cell {
  outbound: string
  /** Подпись ячейки: «Напрямую» или «WARP». */
  label: string
  /** Имя ячейки в выборе выхода и в тостах: «Германия», «Германия · WARP». */
  name: string
  delay: number | null
  warp: boolean
}

export interface Column {
  tag: string
  name: string
  sub: string
  /** Задержка в шапке колонки — по прямому выходу. */
  delay: number | null
  cells: Cell[]
}

const regions = new Intl.DisplayNames(['ru'], { type: 'region' })

/** Россия первой, экзиты по алфавиту тега. */
export const ruFirst = (a: string, b: string) => a === 'ru' ? -1 : b === 'ru' ? 1 : a.localeCompare(b)

// Страна по двухбуквенному тегу экзита: «de» → «Германия»; иной тег - как есть.
export function countryName(tag: string): string {
  const code = tag.replace(/[0-9]+$/, '').toUpperCase()
  if (/^[A-Z]{2}$/.test(code)) {
    try {
      const name = regions.of(code)
      if (name && name !== code) return name
    }
    catch { /* не код страны */ }
  }
  return tag.toUpperCase()
}

// direct-ru не проверяется на задержку: выход в интернет с самого входа.
function ruDelay(delay: number | null, ru: boolean) {
  return ru && delay === null ? 0 : delay
}

/** Колонки доски маршрутов: по колонке на экзит, в ней прямой выход и WARP. */
export function routeColumns(outbounds: Outbound[]): Column[] {
  const byTag = new Map<string, { direct: Outbound | null, warp: Outbound | null }>()
  for (const ob of outbounds) {
    const parsed = parseOutbound(ob.name)
    if (!parsed) continue
    const col = byTag.get(parsed.tag) ?? { direct: null, warp: null }
    col[parsed.variant] = ob
    byTag.set(parsed.tag, col)
  }
  const tags = [...byTag.keys()].sort(ruFirst)
  return tags.map((tag) => {
    const { direct, warp } = byTag.get(tag)!
    const ru = tag === 'ru'
    const name = countryName(tag)
    const cells: Cell[] = []
    if (direct) cells.push({ outbound: direct.name, label: 'Напрямую', name, delay: ruDelay(direct.delay, ru), warp: false })
    if (warp) cells.push({ outbound: warp.name, label: 'WARP', name: `${name} · WARP`, delay: warp.delay, warp: true })
    return { tag, name, sub: ru ? 'RU · entry' : tag.toUpperCase(), delay: direct ? ruDelay(direct.delay, ru) : null, cells }
  })
}

/** Цвет задержки: класс по порогам 60, 100 и 150 мс. */
export const rtc = (ms: number | null) => ms === null ? '' : ms >= 150 ? 'rtp' : ms >= 100 ? 'rta' : ms >= 60 ? 'rtv' : ''
