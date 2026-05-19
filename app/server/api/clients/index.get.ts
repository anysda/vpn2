import { desc } from 'drizzle-orm'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const db = useDb()
  return db.select().from(clients).orderBy(desc(clients.createdAt))
})
