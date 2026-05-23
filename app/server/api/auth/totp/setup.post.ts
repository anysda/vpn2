import { eq } from 'drizzle-orm'
import { useDb } from '../../../database/client'
import { users } from '../../../database/schema'
import { requireAuth } from '../../../utils/auth'
import { buildTotpUri, generateTotpSecret } from '../../../utils/totp'

// Stateless wrt БД: secret НЕ пишется в users до confirm. Иначе
// незавершённый setup (пользователь закрыл вкладку до ввода первого кода)
// лочит логин — он начинает требовать TOTP-код, которого у юзера нет.
// Храним кандидат-секрет в server-side session (`secure` — зашифрованная
// часть cookie, недоступная клиенту).
export default defineEventHandler(async (event) => {
  const u = await requireAuth(event)
  if (u.totpEnabled) {
    throw createError({ statusCode: 409, statusMessage: 'totp_already_enabled' })
  }
  // Защита от рассинхрона: если в БД случайно остался totpSecret (например,
  // от старой версии setup'а до фикса), но в сессии totpEnabled=false —
  // считаем, что enable не завершён, и не блокируем перенастройку.
  const db = useDb()
  const [row] = await db.select().from(users).where(eq(users.id, u.id)).limit(1)
  if (row?.totpSecret && u.totpEnabled) {
    throw createError({ statusCode: 409, statusMessage: 'totp_already_enabled' })
  }

  const secret = generateTotpSecret()

  // Сохраняем кандидат в server-side секции сессии. В БД ничего не трогаем.
  // setUserSession в nuxt-auth-utils использует defu(data, existing) — старые
  // поля сессии (user, loggedInAt и т.п.) сохранятся автоматически, передавать
  // их вручную не нужно.
  await setUserSession(event, {
    secure: { pendingTotpSecret: secret },
  })

  return {
    secret,
    otpauthUrl: buildTotpUri(u.username, secret),
  }
})
