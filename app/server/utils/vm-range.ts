import { useLogger } from './logger'

interface VmRangeResponse {
  status: 'success' | 'error'
  data: { result: Array<{ metric: Record<string, string>, values: Array<[number, string]> }> }
}

/**
 * Ряды query_range по instance: равномерная сетка start..end с шагом step,
 * пропуски VM (нода молчала) — null. Ошибка VM — пустая карта.
 */
export async function rangeQuery(promql: string, start: number, end: number, step: number): Promise<Map<string, Array<number | null>>> {
  const url = useRuntimeConfig().vmUrl as string
  const n = Math.floor((end - start) / step) + 1
  const out = new Map<string, Array<number | null>>()
  try {
    const data = await $fetch<VmRangeResponse>(`${url}/api/v1/query_range`, {
      query: { query: promql, start, end, step: `${step}s` },
      timeout: 5000,
    })
    if (data.status !== 'success') return out
    for (const row of data.data.result) {
      const instance = row.metric.instance
      if (!instance) continue
      const series: Array<number | null> = Array.from({ length: n }, () => null)
      for (const [t, v] of row.values) {
        const i = Math.round((Number(t) - start) / step)
        const value = Number(v)
        if (i >= 0 && i < n && Number.isFinite(value)) series[i] = value
      }
      out.set(instance, series)
    }
  }
  catch (err) {
    useLogger().warn({ err, promql }, 'VM range query failed')
  }
  return out
}
