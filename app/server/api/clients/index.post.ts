import { z } from 'zod'
import { requireAuth } from '../../utils/auth'
import { actorOf } from '../../utils/events'
import { createClient } from '../../utils/client-ops'
import { readBodyAs } from '../../utils/validate'

// Создание клиента: имя, фильтрация, срок действия, лимит девайсов.
// Девайсы добавляются потом, в модалке. Пароль генерится автоматически.
const Body = z.object({
  name: z.string().trim().min(1).max(64),
  filterTraffic: z.boolean().optional(),
  expiresAt: z.iso.datetime().nullable().optional(),
  deviceLimit: z.number().int().min(1).max(999).nullable().optional(),
})

export default defineEventHandler(async (event) => {
  const user = await requireAuth(event)
  const body = await readBodyAs(event, Body)
  return createClient(body, actorOf(user))
})
