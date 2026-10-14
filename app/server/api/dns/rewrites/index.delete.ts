import { z } from 'zod'
import { agh } from '../../../utils/agh-client'
import { requireAuth } from '../../../utils/auth'
import { confirmRewrites, findRewrite } from '../../../utils/dns-context'
import { sameRewrite } from '../../../utils/dns-settings'
import { readBodyAs } from '../../../utils/validate'

const Body = z.object({ domain: z.string().min(1).max(253), answer: z.string().min(1).max(253) })

/** Удалить перезапись. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const { domain, answer } = await readBodyAs(event, Body)
  const cur = await findRewrite(domain, answer)
  await agh('rewrite/delete', { method: 'POST', body: { domain: cur.domain, answer: cur.answer } })
  return confirmRewrites(l => !l.some(x => sameRewrite(x, cur)), `перезапись «${domain}» не удалилась`)
})
