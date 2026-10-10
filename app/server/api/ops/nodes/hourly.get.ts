import { requireAuth } from '../../../utils/auth'
import { fetchTrafficBuckets, nodeInstances } from '../../../utils/vm-client'
import type { TrafficPeriod } from '../../../utils/vm-client'

const PERIODS: readonly TrafficPeriod[] = ['day', 'yesterday', 'week']

// Трафик нод по часам (сутки, вчера) или по суткам (неделя) — для диаграммы
// в карточках «Мониторинга», по тому же селектору периода, что и итоги.
// Отдельно от /api/ops/nodes: тот опрашивается раз в 2с.
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const q = getQuery(event).period
  if (q !== undefined && !PERIODS.includes(q as TrafficPeriod)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_period' })
  }
  const period = (q ?? 'day') as TrafficPeriod
  const nodes = nodeInstances()
  const buckets = await fetchTrafficBuckets(nodes.map(n => n.instance), period)
  return {
    period,
    unit: period === 'week' ? 'day' : 'hour',
    nodes: nodes.map(n => ({ tag: n.tag, buckets: buckets.get(n.instance) ?? [] })),
  }
})
