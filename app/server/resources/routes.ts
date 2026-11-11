import { asc, eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../database/client'
import { devices, routes } from '../database/schema'
import { admin, idParam, notFound } from '../utils/api'
import { fetchProxies, userVisibleOutbounds } from '../utils/clash-client'
import { control } from '../utils/control'
import { actorOf, joinDetail, outboundLabel, recordEvent } from '../utils/events'
import { clientConn, explain, parseQuery, resolveIp } from '../utils/route-check'
import type { RoutePlan } from '../utils/route-match'
import { assertKnownOutbound } from '../utils/route-outbound'
import { detectRouteType, syncRoutesFile } from '../utils/routes-sync'
import { readBodyAs } from '../utils/validate'

const Outbound = z.string().min(1).max(64)
const syncRoutes = () => syncRoutesFile().catch(err => useLogger().error({ err }, 'route sync failed'))

// Снимок пишет роутер рядом с manual-routes.json, перечитываем по смене ревизии.
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

export default {
  'GET /routes': admin(() => useDb().select().from(routes).orderBy(asc(routes.id))),
  'POST /routes': admin(async (event, user) => {
    const body = await readBodyAs(event, z.object({ value: z.string().min(1).max(255), outbound: Outbound }))
    const value = body.value.trim()
    const type = detectRouteType(value)
    if (!type) throw createError({ statusCode: 400, statusMessage: 'invalid_value', message: 'Нужен домен или IPv4/CIDR' })
    await assertKnownOutbound(body.outbound)
    const db = useDb()
    // libsql оборачивает ошибку UNIQUE так, что по тексту её не узнать: 409 даёт только SELECT заранее.
    const dup = await db.select({ id: routes.id }).from(routes).where(eq(routes.value, value)).limit(1)
    if (dup.length > 0) {
      throw createError({ statusCode: 409, statusMessage: 'route_exists', message: `Правило «${value}» уже существует` })
    }
    const [row] = await db.insert(routes).values({ type, value, outbound: body.outbound }).returning()
    await syncRoutes()
    if (row) {
      await recordEvent({
        type: 'route_added', level: 'info', object: `route:${row.id}`, actor: actorOf(user),
        title: `Маршрут добавлен · ${row.value}`, detail: joinDetail(`через ${outboundLabel(row.outbound)}`, actorOf(user)),
      })
    }
    return row
  }),
  'PATCH /routes/:id': admin(async (event, user) => {
    const id = idParam(event)
    const { outbound } = await readBodyAs(event, z.object({ outbound: Outbound }))
    await assertKnownOutbound(outbound)
    const db = useDb()
    const [before] = await db.select({ outbound: routes.outbound }).from(routes).where(eq(routes.id, id)).limit(1)
    const [row] = await db.update(routes).set({ outbound }).where(eq(routes.id, id)).returning()
    if (!row) throw notFound()
    await syncRoutes()
    if (before && before.outbound !== row.outbound) {
      await recordEvent({
        type: 'route_exit', level: 'info', object: `route:${row.id}`, actor: actorOf(user),
        title: `Выход маршрута изменён · ${row.value}`,
        detail: joinDetail(`${outboundLabel(before.outbound)} → ${outboundLabel(row.outbound)}`, actorOf(user)),
      })
    }
    return row
  }),
  'DELETE /routes/:id': admin(async (event, user) => {
    const id = idParam(event)
    const [removed] = await useDb().delete(routes).where(eq(routes.id, id)).returning({ id: routes.id, value: routes.value, outbound: routes.outbound })
    if (!removed) throw notFound()
    await syncRoutes()
    await recordEvent({
      type: 'route_deleted', level: 'info', object: `route:${removed.id}`, actor: actorOf(user),
      title: `Маршрут удалён · ${removed.value}`, detail: joinDetail(`был через ${outboundLabel(removed.outbound)}`, actorOf(user)),
    })
    return { ok: true }
  }),

  /** «Проверка маршрута»: каким выходом уйдёт домен или IP, для всех или для клиента. */
  'GET /routes/check': admin(async (event) => {
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
  }),

  'GET /routes/outbounds': admin(async () => userVisibleOutbounds(await fetchProxies()).map(p => ({
    name: p.name,
    type: p.type,
    // sing-box отдаёт задержку только последней записью history[]; верхний `delay` пуст.
    delay: p.delay ?? p.history?.at(-1)?.delay ?? null,
    isWarp: p.name.endsWith('-warp'),
    isDirectRu: p.name === 'direct-ru',
  }))),
}
