import { z } from 'zod'
import { agh } from '../../utils/agh-client'
import { toggleRules } from '../../utils/agh-parse'
import { requireAuth } from '../../utils/auth'
import { checkUntil, citesGone, filteringStatus, serviceNames } from '../../utils/dns-context'
import { readBodyAs } from '../../utils/validate'

const Body = z.object({
  domain: z.string().trim().toLowerCase().regex(/^[a-z0-9_-]+(\.[a-z0-9_-]+)*\.?$/).max(253),
  block: z.boolean(),
  client: z.string().max(64).optional(),
})

/**
 * «Заблокировать» / «Разблокировать» из строки журнала своим правилом.
 * Сначала снимаем противоположное точное правило; новое добавляем, только если
 * без него AdGuard всё ещё отвечает по-старому. Не помогло и правило (домен
 * держит блокировка сервиса) — свои правила возвращаем как были.
 * Правила AdGuard применяет не сразу, поэтому ответ check_host ждём.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const { domain, block, client } = await readBodyAs(event, Body)
  const d = domain.replace(/\.$/, '')
  const before = (await filteringStatus()).user_rules ?? []
  const set = (rules: string[]) => agh('filtering/set_rules', { method: 'POST', body: { rules } })
  const ok = (r: { reason: string }) => r.reason.startsWith('Filtered') === block

  const t = toggleRules(before, d, block)
  const gone = before.filter(r => !t.rules.includes(r))
  if (t.removed) await set(t.rules)
  // Снятое правило ещё в ответе — движок не пересобран, ждём его.
  let r = await checkUntil(d, client, x => !citesGone(x, gone))
  if (!ok(r)) {
    await set([...t.rules, t.rule])
    r = await checkUntil(d, client, x => ok(x) || (!block && x.reason === 'FilteredBlockedService'))
  }
  const res = { ok: ok(r), r }
  if (!res.ok) {
    await set(before)
    const why = res.r.reason === 'FilteredBlockedService'
      ? `его блокирует сервис «${(await serviceNames()).get(res.r.service_name ?? '') ?? res.r.service_name}»`
      : `AdGuard отвечает ${res.r.reason}`
    throw createError({ statusCode: 409, statusMessage: 'rule_not_enough', message: `Правило для ${d} не помогло: ${why}` })
  }
  return { ok: true }
})
