import { isLoopbackAddr } from '../../utils/bot-api'
import { SSO_PROVIDER } from '../../utils/sso'
import { FIELD_LABELS } from '../../utils/validate'

/**
 * Все ручки входа, сессий и 2FA — better-auth (server/utils/auth.ts).
 *
 * Здесь то, чего библиотека за нас не сделает:
 *
 * 1. Адрес клиента для ограничителя попыток. X-Forwarded-For принимается
 *    только с loopback, то есть от Caddy на этой же ноде (он перезаписывает
 *    заголовок, см. infra/scripts/30-frontend.sh), и берётся последний адрес.
 *    Остальным заголовок заменяем адресом сокета: своё значение на каждую
 *    попытку — и лимит не сработал бы никогда (VPN2-63).
 * 2. Список открытых ручек и проверка Origin — см. OPEN ниже.
 * 3. Вид ошибок. Фронт и бот ждут код в `statusMessage` и русский текст в
 *    `message`, как у остальных ручек панели.
 */

const TEXT: Record<string, string> = {
  INVALID_USERNAME_OR_PASSWORD: 'Неверный логин или пароль',
  INVALID_EMAIL_OR_PASSWORD: 'Неверный логин или пароль',
  INVALID_PASSWORD: 'Неверный текущий пароль',
  INVALID_CODE: 'Код не подошёл',
  TOTP_ALREADY_ENABLED: '2FA уже включена',
  TOTP_NOT_ENABLED: '2FA не включена',
  PASSWORD_LOGIN_DISABLED: 'Вход по паролю отключён',
  PASSWORD_TOO_SHORT: 'Пароль короче 8 символов',
  PASSWORD_TOO_LONG: 'Слишком длинный пароль',
  INVALID_TWO_FACTOR_COOKIE: 'Вход устарел, введите логин и пароль заново',
  TOO_MANY_ATTEMPTS_REQUEST_NEW_CODE: 'Слишком много попыток, введите логин и пароль заново',
  ACCOUNT_TEMPORARILY_LOCKED: 'Слишком много неверных кодов, попробуйте позже',
  TOO_MANY_REQUESTS: 'Слишком много попыток, попробуйте позже',
  UNAUTHORIZED: 'Нужно войти',
  SESSION_EXPIRED: 'Сессия завершена, войдите заново',
}

function clientAddress(event: Parameters<Parameters<typeof defineEventHandler>[0]>[0]): string {
  const peer = event.node.req.socket.remoteAddress ?? ''
  if (isLoopbackAddr(peer)) {
    const forwarded = getRequestHeader(event, 'x-forwarded-for')
      ?.split(',').map(s => s.trim()).filter(Boolean).at(-1)
    return forwarded || peer
  }
  return peer
}

async function humanError(res: Response): Promise<Response> {
  let body: { code?: string, message?: string }
  try {
    body = await res.clone().json()
  }
  catch {
    // тело не JSON — отдаём как есть
    return res
  }
  let code = body.code || (res.status === 429 ? 'TOO_MANY_REQUESTS' : 'UNKNOWN')
  let message = TEXT[code]
  if (code === 'VALIDATION_ERROR') {
    const field = /\[body\.(\w+)/.exec(body.message ?? '')?.[1]
    code = 'INVALID_BODY'
    message = field ? `Проверьте поле «${FIELD_LABELS[field] ?? field}»` : 'Проверьте данные запроса'
  }
  const headers = new Headers(res.headers)
  headers.delete('content-length')
  headers.set('content-type', 'application/json')
  return new Response(JSON.stringify({
    statusCode: res.status,
    statusMessage: code.toLowerCase(),
    message: message ?? body.message ?? code.toLowerCase(),
  }), { status: res.status, headers })
}

/**
 * Наружу открыты только ручки, которыми пользуется панель. Адрес панели
 * библиотека берёт из Host (allowedHosts: ['*']), а с ним доверенным считает
 * любой Origin и любой callbackURL — остальные ручки (сброс пароля, привязка
 * соцсетей, /reset-password/:token с редиректом по callbackURL) стали бы
 * открытым редиректом. Вход через Authentik начинает /auth/authentik прямым
 * вызовом, мимо этого списка.
 */
const OPEN: Record<string, ReadonlySet<string>> = {
  GET: new Set(['/get-session', '/ok', `/callback/${SSO_PROVIDER}`]),
  POST: new Set([
    '/sign-in/username',
    '/sign-out',
    '/change-password',
    '/two-factor/enable',
    '/two-factor/verify-totp',
    '/two-factor/disable',
  ]),
}

/** Запрос со страницы этой же панели: Origin (или Referer) того же хоста. */
function sameOrigin(event: Parameters<Parameters<typeof defineEventHandler>[0]>[0]): boolean {
  const from = getRequestHeader(event, 'origin') || getRequestHeader(event, 'referer')
  if (!from) return false
  try {
    return new URL(from).host === getRequestHeader(event, 'host')
  }
  catch {
    return false
  }
}

export default defineEventHandler(async (event) => {
  const path = event.path.split('?')[0]!.replace(/^\/api\/auth/, '').replace(/\/+$/, '')
  if (!OPEN[event.method]?.has(path)) {
    throw createError({ statusCode: 404, statusMessage: 'not_found' })
  }
  if (event.method !== 'GET' && !sameOrigin(event)) {
    throw createError({ statusCode: 403, statusMessage: 'invalid_origin', message: 'Запрос не со страницы панели' })
  }
  const request = toWebRequest(event)
  const headers = new Headers(request.headers)
  headers.set('x-forwarded-for', clientAddress(event))
  const res = await useAuth().handler(new Request(request, { headers }))
  return res.status >= 400 ? humanError(res) : res
})
