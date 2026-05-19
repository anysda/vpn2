import { z } from 'zod'
import { useDb } from '../../database/client'
import { clients } from '../../database/schema'
import { requireAuth } from '../../utils/auth'
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

  const [row] = await db
    .insert(clients)
    .values({
      name: body.name,
      ssSecret: generateSsSecret(),
      cipher: cfg.ssCipher,
      enabled: true,
      expiresAt: body.expiresAt ? new Date(body.expiresAt) : null,
    })
    .returning()

  await syncShadowsocksConfig().catch((err) => {
    useLogger().error({ err }, 'failed to sync ss config after create')
  })

  return row
})
