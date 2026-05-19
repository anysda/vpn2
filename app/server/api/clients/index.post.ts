import { eq } from 'drizzle-orm'
import { z } from 'zod'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
import { notifyBot } from '../../utils/bot-events'
import { generateSsSecret, syncShadowsocksConfig } from '../../utils/shadowsocks'

const Body = z.object({
  name: z.string().min(1).max(64),
  expiresAt: z.iso.datetime().nullable().optional(),
})

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const body = await readValidatedBody(event, Body.parse)
  const cfg = useRuntimeConfig()
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
      ssSecret: generateSsSecret(),
      cipher: cfg.ssCipher,
      enabled: true,
      expiresAt: body.expiresAt ? new Date(body.expiresAt) : null,
    })
    .returning()

  await syncShadowsocksConfig().catch((err) => {
    useLogger().error({ err }, 'failed to sync ss config after create')
  })

  void notifyBot('client_created', { name: row.name })

  return row
})
