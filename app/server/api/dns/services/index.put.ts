import { z } from 'zod'
import { agh } from '../../../utils/agh-client'
import { requireAuth } from '../../../utils/auth'
import { blockedServices, serviceCatalog } from '../../../utils/dns-context'
import { readBodyAs } from '../../../utils/validate'

const Body = z.object({ ids: z.array(z.string().max(64)).max(1000) })

/** Заблокированные для всех сервисы — список целиком; расписание AdGuard не трогаем. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const ids = [...new Set((await readBodyAs(event, Body)).ids)]
  const known = new Set((await serviceCatalog()).map(s => s.id))
  const unknown = ids.find(id => !known.has(id))
  if (unknown) throw createError({ statusCode: 422, statusMessage: 'unknown_service', message: `Сервиса «${unknown}» нет в AdGuard` })
  const { schedule } = await blockedServices()
  await agh('blocked_services/update', { method: 'PUT', body: { ids, schedule } })
  const now = (await blockedServices()).ids ?? []
  if (now.length !== ids.length || ids.some(id => !now.includes(id))) {
    throw createError({ statusCode: 502, statusMessage: 'agh_not_applied', message: 'AdGuard не применил изменение: блокировка сервисов' })
  }
  return { blocked: now }
})
