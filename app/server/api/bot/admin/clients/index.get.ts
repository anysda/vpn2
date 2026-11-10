import { requireBotAuth } from '../../../../utils/bot-api'
import { clientList } from '../../../../utils/client-view'

/** Список клиентов для админского управления из бота. */
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  return (await clientList()).map(c => ({
    id: c.id,
    name: c.name,
    status: c.status,
    accessStatus: c.accessStatus,
    deviceCount: c.deviceCount,
    deviceLimit: c.deviceLimit,
    expiresAt: c.expiresAt,
    tgLinked: c.tgLinked,
  }))
})
