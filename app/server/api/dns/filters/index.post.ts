import { z } from 'zod'
import { agh } from '../../../utils/agh-client'
import { requireAuth } from '../../../utils/auth'
import { confirmFiltering, findList, listsView } from '../../../utils/dns-context'
import { readBodyAs } from '../../../utils/validate'

const Body = z.object({
  name: z.string().trim().min(1).max(120),
  url: z.string().trim().regex(/^https?:\/\/\S+$/i).max(2048),
  white: z.boolean(),
})

/** Добавить список: AdGuard сам скачивает его и отвечает отказом, если не смог. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const { name, url, white } = await readBodyAs(event, Body)
  await agh('filtering/add_url', { method: 'POST', body: { name, url, whitelist: white }, timeout: 60_000 })
  return listsView(await confirmFiltering(st => !!findList(st, url, white), `список «${name}» не появился`))
})
