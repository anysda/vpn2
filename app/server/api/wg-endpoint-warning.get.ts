import { requireAuth } from '../utils/auth'
import { wgEndpointHasAAAA } from '../utils/wg-endpoint-dns'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  setHeader(event, 'cache-control', 'no-store')
  const cfg = useRuntimeConfig()
  return { hasAAAA: !!cfg.wgEnabled && await wgEndpointHasAAAA(String(cfg.wgPublicHost || '')) }
})
