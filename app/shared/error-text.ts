import table from './error-text.json' with { type: 'json' }

// Единая таблица текстов ошибок: `panel` читает панель (сервер - плагин error-text,
// страница - errText), `bot` и `plural` читает бот.
export function panelErrorText(code: string): string | undefined {
  return Object.hasOwn(table.panel, code) ? (table.panel as Record<string, string>)[code] : undefined
}

export const SESSION_GONE = table.panel.Unauthorized

interface Failure { name?: string, message?: string, statusMessage?: string, data?: { message?: unknown }, response?: { status?: number }, cause?: Failure }

/** HTTP-статус ответа в ошибке ofetch, в том числе завёрнутой Nuxt (useFetch); без ответа - undefined. */
export function failedStatus(e: unknown): number | undefined {
  const x = e as Failure | null
  return (x?.response ?? x?.cause?.response)?.status
}

/**
 * Текст ошибки запроса для тоста и плашки: без ответа - про недоступность панели,
 * иначе русский текст сервера (401 закрытой ручки - «Сессия завершена») или `fallback`.
 * Ошибка не от запроса (своя проверка страницы) отдаёт свой текст.
 */
export function errText(e: unknown, fallback = 'Ошибка'): string {
  const x = e as Failure | null
  const f = x?.cause?.name === 'FetchError' ? x.cause : x
  // Обрыв связи useFetch заворачивает в NuxtError: в cause - TypeError, признак запроса - текст ofetch «[GET] "…"».
  if (f?.name !== 'FetchError' && !/^\[[A-Z]+\] "/.test(x?.message ?? '')) return x?.data?.message as string ?? x?.statusMessage ?? x?.message ?? fallback
  if (!failedStatus(e)) return /TimeoutError|timed? ?out/i.test(`${x?.message} ${x?.cause?.name} ${x?.cause?.message}`) ? table.panel.timeout : table.panel.offline
  const text = [x?.data?.message, f?.data?.message, x?.statusMessage, f?.statusMessage].find(t => typeof t === 'string' && /[а-яё]/i.test(t))
  return text as string | undefined ?? fallback
}
