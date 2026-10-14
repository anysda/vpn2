import { requireAuth } from '../../../utils/auth'
import { rewriteList } from '../../../utils/dns-context'

/** Перезаписи DNS из AdGuard. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  return rewriteList()
})
