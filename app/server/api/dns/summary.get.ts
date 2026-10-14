import { agh } from '../../utils/agh-client'
import { summarize, type AghLogEntry, type AghStats } from '../../utils/agh-parse'
import { requireAuth } from '../../utils/auth'
import { panelNames } from '../../utils/dns-context'

/** Сводка вкладки DNS из статистики AdGuard и последних записей журнала. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const [stats, info, log, names] = await Promise.all([
    agh<AghStats>('stats'),
    agh<{ upstream_mode: string }>('dns_info'),
    agh<{ data: AghLogEntry[] | null }>('querylog', { query: { limit: 1000 } }),
    panelNames(),
  ])
  return summarize(stats, names, info.upstream_mode, log.data ?? [])
})
