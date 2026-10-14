import { z } from 'zod'
import { agh } from '../../../utils/agh-client'
import { requireAuth } from '../../../utils/auth'
import { confirmRewrites, findRewrite } from '../../../utils/dns-context'
import { sameRewrite } from '../../../utils/dns-settings'
import { readBodyAs } from '../../../utils/validate'

const Body = z.object({ domain: z.string().min(1).max(253), answer: z.string().min(1).max(253), enabled: z.boolean() })

/** Включить или выключить перезапись. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const { domain, answer, enabled } = await readBodyAs(event, Body)
  const cur = await findRewrite(domain, answer)
  await agh('rewrite/update', { method: 'PUT', body: { target: cur, update: { domain, answer, enabled } } })
  return confirmRewrites(l => l.some(x => sameRewrite(x, cur) && x.enabled === enabled), `перезапись «${domain}» ${enabled ? 'не включилась' : 'не выключилась'}`)
})
