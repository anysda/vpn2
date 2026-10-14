import { z } from 'zod'
import { agh } from '../../../utils/agh-client'
import { requireAuth } from '../../../utils/auth'
import { confirmFiltering, filteringStatus, findList, listsView } from '../../../utils/dns-context'
import { readBodyAs } from '../../../utils/validate'

const Body = z.object({ url: z.string().min(1).max(2048), white: z.boolean(), enabled: z.boolean() })

/** Включить или выключить список. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const { url, white, enabled } = await readBodyAs(event, Body)
  const f = findList(await filteringStatus(), url, white)
  if (!f) throw createError({ statusCode: 404, statusMessage: 'list_not_found', message: 'Такого списка уже нет в AdGuard' })
  await agh('filtering/set_url', { method: 'POST', body: { url, whitelist: white, data: { name: f.name, url, enabled } }, timeout: 60_000 })
  return listsView(await confirmFiltering(st => findList(st, url, white)?.enabled === enabled, `список «${f.name}» ${enabled ? 'не включился' : 'не выключился'}`))
})
