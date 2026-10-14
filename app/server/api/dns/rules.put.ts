import { z } from 'zod'
import { agh } from '../../utils/agh-client'
import { checkRules } from '../../utils/agh-rules'
import { requireAuth } from '../../utils/auth'
import { confirmFiltering } from '../../utils/dns-context'
import { readBodyAs } from '../../utils/validate'

const Body = z.object({ text: z.string().max(1_000_000) })

/**
 * «Применить» свои правила. Строку с ошибкой AdGuard принял бы и молча
 * пропустил — такой текст не отправляем, человеку номер строки и причина.
 */
export default defineEventHandler(async (event) => {
  await requireAuth(event)
  const lines = (await readBodyAs(event, Body)).text.replace(/\r/g, '').split('\n')
  while (lines.length && !lines.at(-1)!.trim()) lines.pop()
  const errors = checkRules(lines)
  if (errors.length) {
    const e = errors[0]!
    const more = errors.length > 1 ? ` (и ещё ${errors.length - 1})` : ''
    throw createError({
      statusCode: 422,
      statusMessage: 'bad_rules',
      message: `Строка ${e.line}: ${e.why}${more}`,
      data: { message: `Строка ${e.line}: ${e.why}${more}`, errors },
    })
  }
  await agh('filtering/set_rules', { method: 'POST', body: { rules: lines } })
  const st = await confirmFiltering(st => (st.user_rules ?? []).join('\n') === lines.join('\n'), 'свои правила не сохранились')
  return { text: (st.user_rules ?? []).join('\n') }
})
