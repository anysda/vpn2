import { useLogger } from './logger'

interface AghStats {
  num_dns_queries: number
  num_blocked_filtering: number
  num_replaced_safebrowsing: number
  num_replaced_safesearch: number
  num_replaced_parental: number
  avg_processing_time: number
  top_blocked_domains: Array<Record<string, number>>
}

export interface AghSnapshot {
  totalToday: number
  blockedToday: number
  blockRatePct: number
  avgMs: number
  topBlocked: { domain: string, count: number } | null
}

function authHeader(): Record<string, string> {
  const cfg = useRuntimeConfig()
  const user = String(cfg.aghUser ?? '')
  const pass = String(cfg.aghPassword ?? '')
  if (!user || !pass) return {}
  const token = Buffer.from(`${user}:${pass}`).toString('base64')
  return { Authorization: `Basic ${token}` }
}

export async function fetchAdguardStats(): Promise<AghSnapshot | null> {
  const url = useRuntimeConfig().aghUrl as string
  try {
    const data = await $fetch<AghStats>(`${url}/control/stats`, {
      headers: authHeader(),
      timeout: 3000,
    })
    const top = data.top_blocked_domains?.[0]
    const topEntry = top ? Object.entries(top)[0] : null
    return {
      totalToday: data.num_dns_queries ?? 0,
      blockedToday: data.num_blocked_filtering ?? 0,
      blockRatePct: data.num_dns_queries > 0
        ? (data.num_blocked_filtering / data.num_dns_queries) * 100
        : 0,
      avgMs: (data.avg_processing_time ?? 0) * 1000,
      topBlocked: topEntry ? { domain: topEntry[0], count: topEntry[1] } : null,
    }
  }
  catch (err) {
    useLogger().debug({ err }, 'agh /control/stats failed')
    return null
  }
}

interface AghCall {
  method?: 'GET' | 'POST' | 'PUT'
  body?: Record<string, unknown>
  query?: Record<string, string | number | undefined>
  timeout?: number
}

/**
 * Запрос к API AdGuard Home от имени панели. Отказ AdGuard (4xx с текстом)
 * уходит человеку как 400 с его текстом, нет связи или неверные креды — 502.
 */
export async function agh<T>(path: string, call: AghCall = {}): Promise<T> {
  const url = useRuntimeConfig().aghUrl as string
  try {
    // Адрес внешний: типы маршрутов Nitro тут ни при чём, ответ — тот, что ждёт вызов.
    const res: unknown = await $fetch(`${url}/control/${path}`, {
      method: call.method ?? 'GET',
      body: call.body,
      query: call.query,
      headers: authHeader(),
      timeout: call.timeout ?? 8000,
    })
    return res as T
  }
  catch (err) {
    const e = err as { statusCode?: number, data?: unknown }
    const status = e.statusCode ?? 0
    if (status >= 400 && status < 500 && status !== 401 && status !== 403) {
      const text = typeof e.data === 'string' ? e.data.trim() : ''
      throw createError({ statusCode: 400, statusMessage: 'agh_rejected', message: text || 'AdGuard отклонил запрос' })
    }
    useLogger().warn({ err, path }, 'agh request failed')
    throw createError({ statusCode: 502, statusMessage: 'agh_unavailable', message: 'AdGuard недоступен (нет соединения / неверные креды)' })
  }
}
