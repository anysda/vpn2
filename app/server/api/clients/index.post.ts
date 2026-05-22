import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { notifyBot } from '../../utils/bot-events'
import { generateClientPassword } from '../../utils/password'

// Создание клиента: только имя + флаг фильтрации. Девайсы добавляются
// потом, в модалке. Пароль генерится автоматически.
const Body = z.object({
  name: z.string().min(1).max(64),
  filterTraffic: z.boolean().optional(),
})

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const body = await readValidatedBody(event, Body.parse)
  const db = useDb()

  const name = body.name.trim()
  const dup = await db.select({ id: clients.id }).from(clients).where(eq(clients.name, name)).limit(1)
  if (dup.length > 0) {
    throw createError({ statusCode: 409, statusMessage: `Клиент с именем «${name}» уже существует` })
  }

  const [row] = await db
    .insert(clients)
    .values({
      name,
      filterTraffic: body.filterTraffic ?? true,
      password: generateClientPassword(),
    })
    .returning()

  void notifyBot('client_created', { name: row.name })
  return row
})
