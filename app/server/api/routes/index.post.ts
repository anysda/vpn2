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

  const type = detectRouteType(body.value.trim())
  if (!type) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_value (expected domain or ipv4/cidr)' })
  }

  const db = useDb()
  let row
  try {
    [row] = await db
      .insert(routes)
      .values({ type, value: body.value.trim(), outbound: body.outbound })
      .returning()
  }
  catch (e) {
    const err = e as { message?: string }
    if (err.message?.includes('UNIQUE') || err.message?.includes('unique')) {
      throw createError({ statusCode: 409, statusMessage: 'route_already_exists' })
    }
    throw e
  }

  await syncRoutesFile().catch(err => useLogger().error({ err }, 'route sync failed'))
  return row
})
