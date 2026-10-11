import { eq } from 'drizzle-orm'
import { normalizeScreenLayout } from '#shared/screen-layout'
import { useDb } from '../../database/client'
import { users } from '../../database/schema'
import { requireAuth } from '../../utils/auth'

export default defineEventHandler(async (event) => {
  const user = await requireAuth(event)
  const [row] = await useDb()
    .select({ layout: users.screenLayout })
    .from(users)
    .where(eq(users.id, user.id))
    .limit(1)
  return normalizeScreenLayout(row?.layout)
})
