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
 * Валидатор тела для readValidatedBody. С `Schema.parse` h3 кладёт в `message`
 * дамп zod в JSON, и его видел человек в тосте и в Telegram; здесь — одно
 * поле по-русски.
 */
export function zodBody<T>(schema: ZodType<T>) {
  return (body: unknown): T => {
    const r = schema.safeParse(body)
    if (r.success) return r.data
    const key = r.error.issues[0]?.path[0]
    throw new Error(key === undefined ? 'Проверьте данные запроса' : `Проверьте поле «${LABELS[String(key)] ?? String(key)}»`)
  }
}
