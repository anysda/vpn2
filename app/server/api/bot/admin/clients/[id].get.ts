import { requireBotAuth } from '../../../../utils/bot-api'
import { clientCard } from '../../../../utils/client-view'

/** Карточка клиента для админского чата бота. password — для диплинка-приглашения. */
export default defineEventHandler(async (event) => {
  requireBotAuth(event)
  const { client: c, devices, operation, accessStatus, status, tgLinked } = await clientCard(event)
  return {
    operation,
    accessStatus,
    id: c.id,
    name: c.name,
    status,
    frozenManual: c.frozenManual,
    filterTraffic: c.filterTraffic,
    deviceCount: devices.length,
    deviceLimit: c.deviceLimit,
    expiresAt: c.expiresAt,
    tgLinked,
    password: c.password,
  }
})
