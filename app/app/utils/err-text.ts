/** Текст ошибки запроса для человека: из тела ответа, иначе из статуса. */
export function errText(e: unknown, fallback = 'Ошибка'): string {
  const x = e as { data?: { message?: string }, statusMessage?: string, message?: string } | null
  return x?.data?.message ?? x?.statusMessage ?? x?.message ?? fallback
}
