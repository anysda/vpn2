/**
 * Fire-and-forget event to the local Telegram bot HTTP server.
 * The bot lives in another container on the same host (--network host),
 * listening on TGBOT_EVENT_PORT (default 8877).
 */
export async function notifyBot(type: string, payload: Record<string, unknown>): Promise<void> {
  const cfg = useRuntimeConfig()
  const port = Number(cfg.tgbotEventPort ?? 8877)
  const secret = String(cfg.tgbotSecret ?? '')
  if (!secret) return // bot not configured

  try {
    await $fetch(`http://127.0.0.1:${port}/event`, {
      method: 'POST',
      headers: { 'X-Tgbot-Secret': secret },
      body: { type, ...payload },
      timeout: 1500,
    })
  }
  catch {
    // bot not running / refused — silent
  }
}
