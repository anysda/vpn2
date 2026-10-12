/**
 * Cookie авторизует только запросы со страницы панели. SameSite=Lax не
 * защищает от другого порта или поддомена того же сайта. Auth проверяет
 * источник сам; локальный Bearer API бота от браузерных cookie не зависит.
 */
export default defineEventHandler((event) => {
  if (['GET', 'HEAD', 'OPTIONS'].includes(event.method)) return
  const path = event.path.split('?')[0]!
  if (path.startsWith('/api/auth/') || path.startsWith('/api/bot/')) return
  // Без cookie сессии нет: обработчик вернёт обычный 401. Это также
  // сохраняет локальные запросы с Bearer вместо сессии.
  if (!getRequestHeader(event, 'cookie')) return

  const from = getRequestHeader(event, 'origin') ?? getRequestHeader(event, 'referer')
  const host = getRequestHeader(event, 'host')
  const protocol = useRuntimeConfig().session.cookie.secure ? 'https' : 'http'
  if (from && host) {
    try {
      // Протокол задаёт конфигурация панели, Host сохраняет Caddy.
      // X-Forwarded-Host/Proto не принимаем от произвольного клиента.
      if (new URL(from).origin === new URL(`${protocol}://${host}`).origin) return
    }
    catch { /* некорректный или opaque Origin — отказ */ }
  }
  throw createError({ statusCode: 403, statusMessage: 'invalid_origin', message: 'Запрос не со страницы панели' })
})
