import { z } from 'zod'
import { createClient } from '../../../../utils/client-ops'
import { requireBotAuth } from '../../../../utils/bot-api'
import { readBodyAs } from '../../../../utils/validate'

// Создание клиента из админского чата бота — имя + фильтрация.
// Срок/лимит админ задаёт потом через карточку клиента (PATCH).
const Body = z.object({
  name: z.string().min(1).max(64),
  filterTraffic: z.boolean().optional(),
})

export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const body = await readBodyAs(event, Body)
  return createClient(body)
})
