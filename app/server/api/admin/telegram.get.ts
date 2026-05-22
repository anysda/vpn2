import { requireAuth } from '../../utils/auth'
import { botRunning, maskToken, readTelegramRuntime } from '../../utils/telegram-config'

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const cfg = readTelegramRuntime()
  const running = await botRunning()
  return {
    configured: !!cfg.bot_token && !!cfg.chat_id,
    running,
    bot_token_masked: maskToken(cfg.bot_token),
    chat_id: cfg.chat_id,
    admin_username: cfg.admin_username,
  }
})
