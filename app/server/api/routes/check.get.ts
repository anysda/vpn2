import { control } from '../../utils/control'
import { asc, eq } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { devices, routes } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { fetchProxies } from '../../utils/clash-client'
import { clientConn, explain, parseQuery, resolveIp } from '../../utils/route-check'
import type { RoutePlan } from '../../utils/route-match'

// Снимок пишет роутер рядом с manual-routes.json, перечитываем по mtime.
let cached: { revision: string, plan: RoutePlan } | null = null
async function loadPlan(): Promise<RoutePlan> {
  try {
    type Chunk = { revision: string, size: number, data: string }
    let part = await control<Chunk>('routes.plan', { offset: 0, revision: '' })
    if (cached?.revision === part.revision) return cached.plan
    const revision = part.revision
    const chunks = [Buffer.from(part.data, 'base64')]
    let offset = chunks[0]!.length
    if (part.size > 32 * 1024 * 1024) throw new Error('route plan too large')
    while (offset < part.size) {
      part = await control<Chunk>('routes.plan', { offset, revision })
      const chunk = Buffer.from(part.data, 'base64')
      if (!chunk.length) throw new Error('route plan incomplete')
      chunks.push(chunk)
      offset += chunk.length
    }
    cached = { revision, plan: JSON.parse(Buffer.concat(chunks).toString('utf8')) }
    return cached.plan
  }
  catch (err) {
    useLogger().warn({ err }, 'route-plan.json не читается')
    throw createError({ statusCode: 503, statusMessage: 'route_plan_unavailable', message: 'Роутер ещё не выложил маршруты' })
  }
}

// Личный выход клиента роутер выбирает по VPN-адресу устройства (наборы
// client-pref-*), он у всех устройств клиента один. Дорожка lane-* своя у
// каждого адреса - показываем путь первого устройства.
async function clientSource(raw: unknown): Promise<string | undefined> {
  if (raw === undefined || raw === '') return undefined
  const id = Number(raw)
  if (!Number.isInteger(id) || id <= 0) throw createError({ statusCode: 400, statusMessage: 'bad_client', message: 'Нет такого клиента' })
  const list = await useDb().select({ wg: devices.wgIp, ovpn: devices.ovpnIp, ikev2: devices.ikev2Ip })
    .from(devices).where(eq(devices.clientId, id)).orderBy(asc(devices.id))
  return list.map(d => d.wg ?? d.ovpn ?? d.ikev2).find(Boolean) ?? undefined
}

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const query = getQuery(event)
  const q = parseQuery(String(query.q ?? ''))
  if (!q) throw createError({ statusCode: 400, statusMessage: 'bad_query', message: 'Нужен домен или IP' })
  const plan = await loadPlan()
  const [ip, proxies, rows, source] = await Promise.all([
    q.ip ?? resolveIp(q.domain!),
    fetchProxies(),
    useDb().select().from(routes).orderBy(asc(routes.id)),
    clientSource(query.client),
  ])
  return { query: q.shown, ip, ...explain(plan, clientConn(q, ip, source), rows, proxies) }
})
