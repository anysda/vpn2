import { asc } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { routes } from '../../database/schema'
import { requireAuth } from '../../utils/auth'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const db = useDb()
  return db.select().from(routes).orderBy(asc(routes.id))
})
