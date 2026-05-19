import { requireAuth } from '../../utils/auth'

/**
 * Pull per-client traffic counters from outline-ss-server's Prometheus
 * endpoint and aggregate by access_key. Returns { [clientId]: { rxBytes, txBytes } }.
 *
 * Relevant metric:
 *   shadowsocks_data_bytes_per_location{access_key="client_42",dir="p>c"} <bytes>
 *   shadowsocks_data_bytes_per_location{access_key="client_42",dir="c>p"} <bytes>
 *
 * dir="c>p" is client→proxy (upload from client's POV)
 * dir="p>c" is proxy→client (download from client's POV)
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)

  const url = String(useRuntimeConfig().ssPrometheusUrl ?? 'http://127.0.0.1:9091')

  let text: string
  try {
    text = await $fetch<string>(`${url}/metrics`, {
      timeout: 2500,
      responseType: 'text',
    })
  }
  catch {
    return {}
  }

  const result: Record<number, { rxBytes: number, txBytes: number }> = {}
  const re = /shadowsocks_data_bytes(?:_per_location)?\{([^}]+)\}\s+([0-9eE.+-]+)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    const labels = m[1]!
    const value = Number(m[2])
    if (!Number.isFinite(value)) continue

    const akMatch = labels.match(/access_key="client_(\d+)"/)
    if (!akMatch) continue
    const dirMatch = labels.match(/dir="([^"]+)"/)
    if (!dirMatch) continue

    const clientId = Number(akMatch[1])
    const dir = dirMatch[1]
    const slot = (result[clientId] ??= { rxBytes: 0, txBytes: 0 })
    if (dir === 'c>p') slot.txBytes += value
    else if (dir === 'p>c') slot.rxBytes += value
  }

  return result
})
