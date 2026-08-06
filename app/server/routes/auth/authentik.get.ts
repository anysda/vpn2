import { resolveSsoUser, SsoDenied, ssoConfigured, ssoSettings } from '../../utils/sso'

/**
 * Точка входа и callback SSO: GET /auth/authentik.
 *
 * Взят ОБЩИЙ обработчик OIDC (`defineOAuthOidcEventHandler`), а не готовый
 * `defineOAuthAuthentikEventHandler` из той же nuxt-auth-utils: у провайдера
 * `authentik` нет ни state, ни PKCE, ни nonce — то есть нет защиты от login-CSRF
 * и от подмены кода. Общий обработчик делает всё три, а Authentik ими умеет.
 * Ценой одного лишнего запроса за discovery-документом на каждый вход.
 *
 * Всё, что связано с «кого пускаем», живёт в utils/sso.ts — здесь только
 * протокол и посадка в сессию.
 */

const LOGIN_FORM = '/login?direct=1'

const oidcHandler = defineOAuthOidcEventHandler({
  async onSuccess(event, { user: claims }) {
    const log = useLogger()
    const sub = String((claims as Record<string, unknown>)?.sub ?? '')
    const preferred = (claims as Record<string, unknown>)?.preferred_username as string | undefined

    try {
      const user = await resolveSsoUser(sub, preferred)
      await setUserSession(event, {
        user: {
          id: user.id,
          username: user.username,
          totpEnabled: !!user.totpSecret,
          via: 'sso',
        },
      })
      log.info({ sub, username: user.username }, 'sso: вход через Authentik')
      return sendRedirect(event, '/')
    }
    catch (err) {
      if (err instanceof SsoDenied) {
        // Причину пишем в лог, наружу отдаём только код: в браузере человеку
        // незачем знать, есть ли в панели такая учётка.
        log.warn({ sub, preferred, reason: err.message }, 'sso: вход отклонён')
        return sendRedirect(event, `${LOGIN_FORM}&error=${err.code}`)
      }
      log.error({ err, sub }, 'sso: ошибка при посадке в сессию')
      return sendRedirect(event, `${LOGIN_FORM}&error=sso_failed`)
    }
  },

  async onError(event, error) {
    // Сюда приходят отказ на стороне IdP, несовпадение state/nonce и падение
    // обмена кода на токен. Ни в одном из случаев нельзя оставлять человека на
    // экране 500: возвращаем на форму — она и есть break-glass.
    useLogger().error({ err: error }, 'sso: обмен с Authentik не удался')
    return sendRedirect(event, `${LOGIN_FORM}&error=sso_failed`)
  },
})

export default defineEventHandler(async (event) => {
  const { enabled } = ssoSettings()
  if (!enabled || !ssoConfigured()) {
    // Рубильник выключен или клиент недонастроен — молча возвращаем на форму.
    // Ровно этот путь не даёт зациклиться: middleware шлёт сюда, мы шлём на
    // /login?direct=1, а там авторедирект уже не срабатывает.
    useLogger().warn({ enabled, configured: ssoConfigured() }, 'sso: запрос при выключенном SSO')
    return sendRedirect(event, `${LOGIN_FORM}&error=sso_off`)
  }
  return oidcHandler(event)
})
