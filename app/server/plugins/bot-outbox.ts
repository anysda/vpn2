export default defineNitroPlugin(nitro =>
  every(nitro, 2000, () => retryBotEvents().catch(err => useLogger().error({ err }, 'bot outbox retry failed'))))
