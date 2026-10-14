import { z } from 'zod'
import { agh } from '../../utils/agh-client'
import { logMatch, logRow, type AghLogEntry, type LogRow } from '../../utils/agh-parse'
import { requireAuth } from '../../utils/auth'
import { filteringStatus, listNames, panelNames, serviceNames } from '../../utils/dns-context'

const Query = z.object({
  f: z.enum(['all', 'blk', 'ok', 'rw']).default('all'),
  q: z.string().trim().max(200).default(''),
  older: z.string().max(64).optional(),
})

const PAGE = 500
const SCAN = 10
const WANT = 100

/**
 * Журнал запросов. Фильтр и поиск точные: поиск идёт и по именам клиентов
 * панели, которых AdGuard не знает, поэтому страницы AdGuard дочитываются
 * здесь — до WANT строк или SCAN страниц; `older` продолжает с места остановки.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const p = Query.safeParse(getQuery(event))
  if (!p.success) throw createError({ statusCode: 400, statusMessage: 'invalid_query', message: 'Проверьте фильтр журнала' })
  const { f, q } = p.data
  const [names, st, services] = await Promise.all([panelNames(), filteringStatus(), serviceNames()])
  const lists = listNames(st)
  const byName = q && [...names.values()].some(n => n.toLowerCase().includes(q.toLowerCase()))
  const rows: LogRow[] = []
  let older = p.data.older
  for (let i = 0; i < SCAN && rows.length < WANT; i++) {
    const page = await agh<{ data: AghLogEntry[] | null, oldest: string }>('querylog', {
      query: {
        limit: PAGE,
        older_than: older,
        response_status: f === 'blk' || f === 'rw' ? 'filtered' : 'all',
        search: q && !byName ? q : undefined,
      },
    })
    const data = page.data ?? []
    for (const e of data) {
      const r = logRow(e, names, lists, services)
      if (logMatch(r, f, q)) rows.push(r)
    }
    older = data.length < PAGE || !page.oldest ? undefined : page.oldest
    if (!older) break
  }
  return { rows, older: older ?? null }
})
