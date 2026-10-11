import { z } from 'zod'
import { requireAuth } from '../../../utils/auth'
import { discoveryFrom, probeDiscovery } from '../../../utils/sso'
import { readBodyAs } from '../../../utils/validate'

const Body = z.object({
  authentikUrl: z.string().trim(),
  appSlug: z.string().trim().default(''),
})

/** Кнопка «Проверить»: читает discovery по введённым, ещё не сохранённым полям. */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const body = await readBodyAs(event, Body)
  const url = discoveryFrom(body.authentikUrl, body.appSlug)
  if (!/^https?:\/\//.test(url)) {
    throw createError({ statusCode: 400, statusMessage: 'sso_bad_url', message: 'Адрес Authentik должен начинаться с https://' })
  }
  const r = await probeDiscovery(url)
  if (!r.ok) throw createError({ statusCode: 400, statusMessage: 'sso_discovery_failed', message: r.message })
  return { ok: true, issuer: r.issuer, discoveryUrl: url }
})
