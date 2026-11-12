const PRUNE_MS = 3600_000

// Журнал событий: сторож нод, failover и напоминания о сроке раз в 30 с, чистка старше 30 дней раз в час.
// До миграций таблицы журнала может не быть: every ждёт готовности панели.
export default defineNitroPlugin((nitro) => {
  const log = useLogger()
  let prunedAt = 0
  every(nitro, NODE_CHECK_MS, async () => {
    await checkNodes().catch(err => log.debug({ err }, 'events: node check skipped'))
    await checkFailover().catch(err => log.warn({ err }, 'events: failover check failed'))
    await remindExpiring().catch(err => log.error({ err }, 'events: expiry reminder failed'))
    if (Date.now() - prunedAt < PRUNE_MS) return
    prunedAt = Date.now()
    await pruneEvents().catch(err => log.error({ err }, 'events: prune failed'))
  }, 5_000)
})
