import { z } from 'zod'
import { requireAuth } from '../../utils/auth'
import { createClient } from '../../utils/client-ops'

// Создание клиента: имя, фильтрация, срок действия, лимит девайсов.
// Девайсы добавляются потом, в модалке. Пароль генерится автоматически.
const Body = z.object({
  name: z.string().min(1).max(64),
  filterTraffic: z.boolean().optional(),
  expiresAt: z.iso.datetime().nullable().optional(),
  deviceLimit: z.number().int().min(1).max(999).nullable().optional(),
})

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const body = await readValidatedBody(event, Body.parse)
  return createClient(body)
})
