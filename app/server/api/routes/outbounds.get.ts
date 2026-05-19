import { fetchProxies, userVisibleOutbounds } from '../../utils/clash-client'
import { requireAuth } from '../../utils/auth'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const proxies = await fetchProxies()
  const visible = userVisibleOutbounds(proxies)
  return visible.map((p) => {
    // sing-box exposes delay only via history[] (latest entry); top-level
    // `delay` is empty.
    const latestDelay = p.history && p.history.length > 0
      ? p.history[p.history.length - 1]!.delay
      : null
    return {
      name: p.name,
      type: p.type,
      delay: p.delay ?? latestDelay,
      isWarp: p.name.endsWith('-warp'),
      isDirectRu: p.name === 'direct-ru',
    }
  })
})
