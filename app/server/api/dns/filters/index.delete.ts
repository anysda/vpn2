import { z } from 'zod'
import { agh } from '../../../utils/agh-client'
import { requireAuth } from '../../../utils/auth'
import { confirmFiltering, listsView, findList } from '../../../utils/dns-context'
import { readBodyAs } from '../../../utils/validate'

const Body = z.object({ url: z.string().min(1).max(2048), white: z.boolean() })

export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const { url, white } = await readBodyAs(event, Body)
  await agh('filtering/remove_url', { method: 'POST', body: { url, whitelist: white } })
  return listsView(await confirmFiltering(st => !findList(st, url, white), 'список остался'))
})
