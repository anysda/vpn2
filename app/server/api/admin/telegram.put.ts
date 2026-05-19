import { z } from 'zod'
import { requireAuth } from '../../utils/auth'
import { readTelegramRuntime, writeTelegramRuntime } from '../../utils/telegram-config'

const Body = z.object({
  bot_token: z.string().optional(),
  chat_id: z.union([z.string(), z.number()]).optional(),
})

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const body = await readValidatedBody(event, Body.parse)

  const cur = readTelegramRuntime()
  const next = {
    bot_token: body.bot_token !== undefined ? body.bot_token : cur.bot_token,
    chat_id: body.chat_id !== undefined ? String(body.chat_id) : cur.chat_id,
  }

  writeTelegramRuntime(next)
  useLogger().info({ chat_id: next.chat_id }, 'telegram runtime updated')

  return { ok: true }
})
