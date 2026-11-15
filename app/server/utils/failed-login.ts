import { eq } from 'drizzle-orm'
import { useDb } from '../database/client'
import { events } from '../database/schema'
import { pluralRu } from '../../shared/plural'

const FAILED_LOGIN_WINDOW_MS = 10 * 60_000
const failedLogins = new Map<string, { first: number, count: number, row: Promise<number | undefined> }>()

/** Перебор не вытесняет ленту: попытки с адреса за 10 минут от первой - одна строка со счётчиком. */
export async function journalFailedLogin(ip: string, reason: string): Promise<void> {
  const now = Date.now()
  for (const [addr, run] of failedLogins) if (now - run.first > FAILED_LOGIN_WINDOW_MS) failedLogins.delete(addr)
  const run = failedLogins.get(ip)
  const detail = joinDetail(reason, ip)
  if (!run) {
    const row = useDb().insert(events).values({ at: now, type: 'login_failed', level: 'alarm', title: 'Неудачный вход в панель', detail })
      .returning({ id: events.id }).then(([r]) => r?.id)
    failedLogins.set(ip, { first: now, count: 1, row: row.catch(() => undefined) })
    await row.catch(err => useLogger().error({ err, type: 'login_failed' }, 'event journal write failed'))
    return
  }
  const count = ++run.count
  const id = await run.row
  if (!id) {
    // Первая запись не легла: серия начинается заново своей строкой.
    if (failedLogins.get(ip) === run) failedLogins.delete(ip)
    return journalFailedLogin(ip, reason)
  }
  const sec = Math.max(1, Math.round((now - run.first) / 1000))
  const title = `${count} ${pluralRu(count, ['неудачный вход', 'неудачных входа', 'неудачных входов'])} за ${sec < 60 ? `${sec} с` : `${Math.round(sec / 60)} мин`}`
  // Счётчик растёт синхронно: при параллельных попытках последней ложится строка с наибольшим.
  if (count === run.count) {
    await useDb().update(events).set({ at: now, title, detail }).where(eq(events.id, id))
      .catch(err => useLogger().error({ err, type: 'login_failed' }, 'event journal write failed'))
  }
}
