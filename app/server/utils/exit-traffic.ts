import { MSK_OFFSET_SEC, mskDay } from './msk-day'
import { DAILY_DEVICE_FILTER, instantQuery, nodeInstances } from './vm-client'
import { rangeQuery } from './vm-range'

export type TrafficPeriod = 'day' | 'week'

export interface ExitTraffic {
  period: TrafficPeriod
  /** Шаг ряда, секунды: час для суток, сутки МСК для недели. */
  step: number
  /** Начала вёдер, unix-секунды, старое первым; последнее ведро — текущее, неполное. */
  starts: number[]
  /** Байты по экзиту за ведро; null — VM ничего не отдала. */
  exits: Array<{ tag: string, bytes: Array<number | null> }>
  /** Сумма экзитов за ведро; пропуск экзита считается нулём, ряд не рвётся. */
  total: number[]
  /** Всего за окно графика. */
  sum: number
  /** То же окно периодом раньше (вчера в это же время / неделю назад); null — нет данных. */
  prev: number | null
}

// Трафик экзита — приём на физическом WAN: через него идут и запросы клиента,
// и ответы из интернета. Тот же фильтр, что у суточного трафика нод.
const RX = `node_network_receive_bytes_total{${DAILY_DEVICE_FILTER}}`
const increase = (sec: number) => `sum by(instance) (increase_prometheus(${RX}[${sec}s]))`

const round = (v: number | null | undefined) => v == null ? null : Math.round(v)

const SHAPE = { day: { step: 3600, n: 24 }, week: { step: 86400, n: 7 } } as const

/** Ряды трафика по экзитам за сутки (24 часа) или неделю (7 суток МСК). */
export async function exitTraffic(period: TrafficPeriod, nowMs = Date.now()): Promise<ExitTraffic> {
  const { step, n } = SHAPE[period]
  const now = Math.floor(nowMs / 1000)
  const cur = period === 'day' ? Math.floor(now / 3600) * 3600 : mskDay(nowMs) * 86400 - MSK_OFFSET_SEC
  const first = cur - (n - 1) * step
  const elapsed = now - cur
  const window = now - first
  const exits = nodeInstances().filter(x => x.tag !== 'ru')

  // Полные вёдра — query_range: точка в конце ведра считает прирост за него.
  // Текущее ведро — мгновенный запрос с окном от его начала до сейчас.
  const [full, partial, before] = await Promise.all([
    rangeQuery(increase(step), first + step, cur, step),
    elapsed > 0 ? instantQuery(increase(elapsed), now) : Promise.resolve(new Map<string, number>()),
    instantQuery(increase(window), now - n * step),
  ])

  const rows = exits.map(({ tag, instance }) => {
    const done = full.get(instance) ?? []
    const bytes = Array.from({ length: n - 1 }, (_, i) => round(done[i]))
    bytes.push(elapsed > 0 ? round(partial.get(instance)) : 0)
    return { tag, bytes }
  })
  const total = Array.from({ length: n }, (_, i) => rows.reduce((s, r) => s + (r.bytes[i] ?? 0), 0))
  const prevs = exits.map(x => round(before.get(x.instance))).filter((v): v is number => v !== null)

  return {
    period,
    step,
    starts: Array.from({ length: n }, (_, i) => first + i * step),
    exits: rows,
    total,
    sum: total.reduce((a, b) => a + b, 0),
    prev: prevs.length ? prevs.reduce((a, b) => a + b, 0) : null,
  }
}
