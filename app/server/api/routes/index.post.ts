import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { routes } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { detectRouteType, syncRoutesFile } from '../../utils/routes-sync'

const Body = z.object({
  value: z.string().min(1).max(255),
  outbound: z.string().min(1).max(64),
})

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const body = await readValidatedBody(event, Body.parse)

  const value = body.value.trim()
  const type = detectRouteType(value)
  if (!type) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_value (expected domain or ipv4/cidr)' })
  }

  const db = useDb()
  // Explicit pre-check — libsql wraps the UNIQUE constraint error so it
  // can't be matched on err.message; a SELECT is the reliable way to 409.
  const dup = await db.select({ id: routes.id }).from(routes).where(eq(routes.value, value)).limit(1)
  if (dup.length > 0) {
    throw createError({ statusCode: 409, statusMessage: `Правило «${value}» уже существует` })
  }

  const [row] = await db
    .insert(routes)
    .values({ type, value, outbound: body.outbound })
    .returning()

  await syncRoutesFile().catch(err => useLogger().error({ err }, 'route sync failed'))
  return row
})
