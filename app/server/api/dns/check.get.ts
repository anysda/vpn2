import { domainToASCII } from 'node:url'
import { z } from 'zod'
import { agh } from '../../utils/agh-client'
import type { CheckHost } from '../../utils/agh-parse'
import { verdict } from '../../utils/agh-rules'
import { requireAuth } from '../../utils/auth'
import { filteringStatus, listNames, serviceNames } from '../../utils/dns-context'

const Query = z.object({ name: z.string().trim().toLowerCase().transform(s => domainToASCII(s.replace(/\.$/, '')) || s).pipe(z.string().regex(/^[a-z0-9_*.-]+$/).max(253)) })

/** «Проверка фильтрации»: что AdGuard ответит на запрос этого домена. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const q = Query.safeParse(getQuery(event))
  if (!q.success) throw createError({ statusCode: 400, statusMessage: 'bad_domain', message: 'Это не похоже на домен' })
  const [r, st, services] = await Promise.all([
    agh<CheckHost>('filtering/check_host', { query: { name: q.data.name } }),
    filteringStatus(),
    serviceNames(),
  ])
  return verdict(r, listNames(st), services)
})
