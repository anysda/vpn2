import { eq } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { routes } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { actorOf, joinDetail, outboundLabel, recordEvent } from '../../utils/events'
import { syncRoutesFile } from '../../utils/routes-sync'

export default defineEventHandler(async (event) => {
  const user = await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const db = useDb()
  const result = await db.delete(routes).where(eq(routes.id, id)).returning({ id: routes.id, value: routes.value, outbound: routes.outbound })
  if (result.length === 0) {
    throw createError({ statusCode: 404, statusMessage: 'not_found' })
  }

  await syncRoutesFile().catch(err => useLogger().error({ err }, 'route sync failed'))
  const [removed] = result
  await recordEvent({
    type: 'route_deleted', level: 'info', object: `route:${removed!.id}`, actor: actorOf(user),
    title: `Маршрут удалён · ${removed!.value}`, detail: joinDetail(`был через ${outboundLabel(removed!.outbound)}`, actorOf(user)),
  })
  return { ok: true }
})
