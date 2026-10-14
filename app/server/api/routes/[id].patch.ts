import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { routes } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { actorOf, joinDetail, outboundLabel, recordEvent } from '../../utils/events'
import { assertKnownOutbound } from '../../utils/route-outbound'
import { syncRoutesFile } from '../../utils/routes-sync'
import { readBodyAs } from '../../utils/validate'

const Body = z.object({ outbound: z.string().min(1).max(64) })

export default defineEventHandler(async (event) => {
  const user = await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const body = await readBodyAs(event, Body)
  await assertKnownOutbound(body.outbound)
  const db = useDb()
  const [before] = await db.select({ outbound: routes.outbound }).from(routes).where(eq(routes.id, id)).limit(1)

  const [row] = await db
    .update(routes)
    .set({ outbound: body.outbound })
    .where(eq(routes.id, id))
    .returning()

  if (!row) {
    throw createError({ statusCode: 404, statusMessage: 'not_found' })
  }

  await syncRoutesFile().catch(err => useLogger().error({ err }, 'route sync failed'))
  if (before && before.outbound !== row.outbound) {
    await recordEvent({
      type: 'route_exit', level: 'info', object: `route:${row.id}`, actor: actorOf(user),
      title: `Выход маршрута изменён · ${row.value}`,
      detail: joinDetail(`${outboundLabel(before.outbound)} → ${outboundLabel(row.outbound)}`, actorOf(user)),
    })
  }
  return row
})
