import { LOGIN_FORM, resetAuth, useAuth } from '../../utils/auth'
import { SSO_PROVIDER, ssoConfigured, ssoSettings } from '../../utils/sso'

/**
 * Точка входа и адрес возврата SSO: GET /auth/authentik.
 *
 * Протокол (state, PKCE, nonce, обмен кода) ведёт better-auth. Адрес возврата
 * у провайдера в Authentik прежний — /auth/authentik (NUXT_OAUTH_OIDC_REDIRECT_URL),
 * поэтому пришедший с кодом запрос пересылаем в колбэк библиотеки как есть.
 */
export default defineEventHandler(async (event) => {
  const s = ssoSettings()
  if (!s.enabled || !ssoConfigured(s)) {
    // Рубильник выключен или клиент недонастроен — молча возвращаем на форму.
    // Ровно этот путь не даёт зациклиться: middleware шлёт сюда, мы шлём на
    // /login?direct=1, а там авторедирект уже не срабатывает.
    useLogger().warn({ enabled: s.enabled, configured: ssoConfigured(s) }, 'sso: запрос при выключенном SSO')
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
    // Authentik недоступен или discovery не прочитался — форма и есть break-glass.
    // Discovery библиотека читает один раз при сборке экземпляра: пересобираем,
    // чтобы следующая попытка прочитала его заново, а не до рестарта панели.
    useLogger().error({ err }, 'sso: не удалось начать вход через Authentik')
    resetAuth()
    return sendRedirect(event, `${LOGIN_FORM}&error=sso_failed`)
  }
})
