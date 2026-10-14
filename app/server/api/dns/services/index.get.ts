import { requireAuth } from '../../../utils/auth'
import { blockedServices, serviceCatalog } from '../../../utils/dns-context'

/** Каталог сервисов AdGuard и заблокированные для всех клиентов. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const [all, blocked] = await Promise.all([serviceCatalog(), blockedServices()])
  return { all: all.map(s => ({ id: s.id, name: s.name })), blocked: blocked.ids ?? [] }
})
