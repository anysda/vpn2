import { z } from 'zod'
import { agh } from '../../../utils/agh-client'
import { requireAuth } from '../../../utils/auth'
import { confirmRewrites, rewriteList } from '../../../utils/dns-context'
import { checkRewrite, sameRewrite } from '../../../utils/dns-settings'
import { readBodyAs } from '../../../utils/validate'

const Body = z.object({ domain: z.string().max(253), answer: z.string().max(253) })

/** Добавить перезапись. Дубль AdGuard принимает молча — отказываем сами. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const b = await readBodyAs(event, Body)
  const rw = { domain: b.domain.trim().toLowerCase().replace(/\.$/, ''), answer: b.answer.trim() }
  const bad = checkRewrite(rw.domain, rw.answer)
  if (bad) throw createError({ statusCode: 422, statusMessage: 'bad_rewrite', message: bad })
  if ((await rewriteList()).some(x => sameRewrite(x, rw))) {
    throw createError({ statusCode: 409, statusMessage: 'rewrite_exists', message: `Перезапись «${rw.domain}» → ${rw.answer} уже есть` })
  }
  await agh('rewrite/add', { method: 'POST', body: { ...rw, enabled: true } })
  return confirmRewrites(l => l.some(x => sameRewrite(x, rw)), `перезапись «${rw.domain}» не появилась`)
})
