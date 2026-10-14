import { agh } from '../../../utils/agh-client'
import { requireAuth } from '../../../utils/auth'
import { filteringStatus, listsView } from '../../../utils/dns-context'

/** «Проверить обновления»: AdGuard перекачивает включённые списки, чёрные и белые. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  let updated = 0
  for (const whitelist of [false, true]) {
    updated += (await agh<{ updated: number }>('filtering/refresh', { method: 'POST', body: { whitelist }, timeout: 120_000 })).updated ?? 0
  }
  return { updated, ...listsView(await filteringStatus()) }
})
