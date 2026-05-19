import { useLogger } from './logger'

export interface ClashProxy {
  name: string
  type: string
  now?: string
  delay?: number
  history?: Array<{ time: string, delay: number }>
}

interface ProxiesResponse {
  proxies: Record<string, ClashProxy>
}

interface ConnectionsResponse {
  connections: Array<{
    chains: string[]
    upload: number
    download: number
    metadata: {
      destinationIP: string
      destinationPort: string
      host: string
      network: string
      sourceIP: string
      sourcePort: string
      type: string
    }
  }>
  uploadTotal: number
  downloadTotal: number
  memory: number
}

function authHeaders(): Record<string, string> {
  const secret = useRuntimeConfig().clashSecret as string
  return secret ? { Authorization: `Bearer ${secret}` } : {}
}

// sing-box's clash API serves JSON with `Content-Type: text/plain`, so $fetch's
// auto-parser leaves it as a string. Force JSON parsing.
const FETCH_OPTS = {
  parseResponse: JSON.parse,
  timeout: 3000,
} as const

export async function fetchProxies(): Promise<ClashProxy[]> {
  const url = useRuntimeConfig().clashApiUrl as string
  try {
    const data = await $fetch<ProxiesResponse>(`${url}/proxies`, {
      ...FETCH_OPTS,
      headers: authHeaders(),
    })
    return Object.values(data.proxies ?? {})
  }
  catch (err) {
    useLogger().warn({ err }, 'clash /proxies failed')
    return []
  }
}

export async function fetchConnections(): Promise<ConnectionsResponse | null> {
  const url = useRuntimeConfig().clashApiUrl as string
  try {
    return await $fetch<ConnectionsResponse>(`${url}/connections`, {
      ...FETCH_OPTS,
      headers: authHeaders(),
    })
  }
  catch (err) {
    useLogger().warn({ err }, 'clash /connections failed')
    return null
  }
}

/**
 * The proxies we expose as drag targets in the panel: anything starting with
 * `hy2-` (per-exit, direct or warp) plus `direct-ru`. Skips internal urltest
 * groups (`*-best`, `block-out`, etc.).
 */
export function userVisibleOutbounds(proxies: ClashProxy[]): ClashProxy[] {
  return proxies
    .filter(p => p.name === 'direct-ru' || p.name.startsWith('hy2-'))
    .sort((a, b) => {
      // direct-ru first, then per-exit direct, then per-exit warp
      if (a.name === 'direct-ru') return -1
      if (b.name === 'direct-ru') return 1
      const aWarp = a.name.endsWith('-warp')
      const bWarp = b.name.endsWith('-warp')
      if (aWarp !== bWarp) return aWarp ? 1 : -1
      return a.name.localeCompare(b.name)
    })
}
