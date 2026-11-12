/**
 * Точка входа и адрес возврата SSO. Протокол ведёт better-auth; запрос с кодом
 * пересылаем в её колбэк: адрес возврата у провайдера - /auth/authentik.
 */
export default defineEventHandler(async (event) => {
  if (!ssoSettings().enabled) {
    // На форму с direct=1: там авторедирект не срабатывает, петли нет.
    useLogger().warn('sso: запрос при выключенном SSO')
    return sendRedirect(event, `${LOGIN_FORM}&error=sso_off`)
  }

  const query = getQuery(event)
  if (query.code || query.error) {
    const qs = new URLSearchParams(query as Record<string, string>).toString()
    return sendRedirect(event, `/api/auth/callback/${SSO_PROVIDER}?${qs}`)
  }

  try {
    const { headers, response } = await useAuth().api.signInSocial({
      body: { provider: SSO_PROVIDER, callbackURL: '/', errorCallbackURL: LOGIN_FORM, disableRedirect: true },
      headers: event.headers,
      returnHeaders: true,
    })
    if (!response.url) throw new Error('better-auth не вернул адрес IdP')
    for (const cookie of headers.getSetCookie()) appendResponseHeader(event, 'set-cookie', cookie)
    return sendRedirect(event, response.url)
  }
  catch (err) {
    // Форма и есть break-glass. Экземпляр пересобираем: discovery читается при сборке.
    useLogger().error({ err }, 'sso: не удалось начать вход через Authentik')
    resetAuth()
    return sendRedirect(event, `${LOGIN_FORM}&error=sso_failed`)
  }
})
