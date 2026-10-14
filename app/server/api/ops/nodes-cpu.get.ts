import { requireAuth } from '../../utils/auth'
import { PROMQL, nodeInstances } from '../../utils/vm-client'
import { rangeQuery } from '../../utils/vm-range'

// Спарклайн узла на обзоре: 13 точек с шагом опроса панели (2 с), дальше
// страница сама дописывает свежие значения из /api/ops/nodes.
const POINTS = 13
const STEP = 2

/** Недавняя загрузка CPU по узлам: { tag: [старое … новое] }, пропуск — null. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const end = Math.floor(Date.now() / 1000 / STEP) * STEP
  const series = await rangeQuery(PROMQL.cpu, end - (POINTS - 1) * STEP, end, STEP)
  return Object.fromEntries(nodeInstances().map(n => [n.tag, series.get(n.instance) ?? []]))
})
