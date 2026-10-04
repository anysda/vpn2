import type { ZodType } from 'zod'

// Подписи полей для человека; поля без подписи называются как в запросе.
const LABELS: Record<string, string> = {
  name: 'Имя',
  value: 'Правило',
  outbound: 'Выход',
  username: 'Логин',
  password: 'Пароль',
  currentPassword: 'Текущий пароль',
  newPassword: 'Новый пароль',
  code: 'Код',
  totpCode: 'Код',
  expiresAt: 'Срок действия',
  deviceLimit: 'Лимит устройств',
}

/**
 * Тело запроса по схеме zod. readValidatedBody отдаёт человеку английское
 * «Invalid JSON body», а с `Schema.parse` — дамп zod в JSON; оба доходили до
 * тоста и до Telegram. Здесь код в `statusMessage`, текст по-русски в `message`.
 */
export async function readBodyAs<T>(event: Parameters<typeof readBody>[0], schema: ZodType<T>): Promise<T> {
  let body: unknown
  try {
    body = await readBody(event, { strict: true })
  }
  catch (e) {
    if (isError(e) && e.statusCode === 400) {
      throw createError({ statusCode: 400, statusMessage: 'invalid_json', message: 'Запрос не в формате JSON' })
    }
    throw e
  }
  const r = schema.safeParse(body)
  if (r.success) return r.data
  const key = r.error.issues[0]?.path[0]
  throw createError({
    statusCode: 400,
    statusMessage: 'invalid_body',
    message: key === undefined ? 'Проверьте данные запроса' : `Проверьте поле «${LABELS[String(key)] ?? String(key)}»`,
  })
}
