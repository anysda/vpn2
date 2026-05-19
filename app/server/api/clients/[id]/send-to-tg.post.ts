import { eq } from 'drizzle-orm'
import { useDb } from '../../../database/client'
import { clients } from '../../../database/schema'
import { requireAuth } from '../../../utils/auth'
import { buildSsUrl } from '../../../utils/ss-url'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const id = Number(getRouterParam(event, 'id'))
  if (!Number.isFinite(id)) {
    throw createError({ statusCode: 400, statusMessage: 'invalid_id' })
  }

  const cfg = useRuntimeConfig()
  if (!cfg.ssPublicHost) {
    throw createError({ statusCode: 500, statusMessage: 'ss_public_host_not_configured' })
  }

  const secret = String(cfg.tgbotSecret ?? '')
  if (!secret) {
    throw createError({ statusCode: 503, statusMessage: 'Telegram-бот не настроен' })
  }

  const db = useDb()
  const [row] = await db.select().from(clients).where(eq(clients.id, id)).limit(1)
  if (!row) {
    throw createError({ statusCode: 404, statusMessage: 'not_found' })
  }

  const ssUrl = buildSsUrl({
    cipher: row.cipher,
    secret: row.ssSecret,
    host: cfg.ssPublicHost,
    port: Number(cfg.ssPort),
    name: row.name,
  })

  const port = Number(cfg.tgbotEventPort ?? 8877)
  try {
    await $fetch(`http://127.0.0.1:${port}/event`, {
      method: 'POST',
      headers: { 'X-Tgbot-Secret': secret },
      body: { type: 'client_send_config', name: row.name, ssUrl },
      timeout: 5000,
    })
  }
  catch (e) {
    throw createError({
      statusCode: 502,
      statusMessage: 'Бот не отвечает — проверь /api/admin/telegram',
      cause: e,
    })
  }

  return { ok: true }
})
