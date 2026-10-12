import { retryBotEvents } from '../utils/bot-events'

export default defineNitroPlugin((nitro) => {
  let running = false
  const timer = setInterval(async () => {
    if (running) return
    running = true
    try { await retryBotEvents() }
    catch (err) { useLogger().error({ err }, 'bot outbox retry failed') }
    finally { running = false }
  }, 2000)
  nitro.hooks.hook('close', () => clearInterval(timer))
})
