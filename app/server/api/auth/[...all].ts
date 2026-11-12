import type { H3Event } from 'h3'
import errorText from '../../../shared/error-text.json' with { type: 'json' }

// Вход, сессии, 2FA и лимит попыток ведёт better-auth (utils/auth.ts). Здесь: адрес клиента
// (X-Forwarded-For только от Caddy этой ноды), открытые ручки с проверкой Origin, запись неудач
// в журнал и вид ошибок: код в `statusMessage`, русский текст в `message`.

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
  UNAUTHORIZED: errorText.panel.Unauthorized,
  SESSION_EXPIRED: 'Сессия завершена, войдите заново',
}

/**
 * Открыты только ручки, которыми пользуется панель: библиотека доверяет любому
 * Origin и callbackURL (allowedHosts: ['*']), остальные стали бы открытым редиректом.
 */
const OPEN: Record<string, ReadonlySet<string>> = {
  GET: new Set(['/get-session', '/ok', `/callback/${SSO_PROVIDER}`]),
  POST: new Set(['/sign-in/username', '/sign-out', '/change-password', '/two-factor/enable', '/two-factor/verify-totp', '/two-factor/disable']),
}

function clientAddress(event: H3Event): string {
  const peer = event.node.req.socket.remoteAddress ?? ''
  if (!isLoopbackAddr(peer)) return peer
  return getRequestHeader(event, 'x-forwarded-for')?.split(',').map(s => s.trim()).filter(Boolean).at(-1) || peer
}

async function humanError(res: Response): Promise<Response> {
  const body = await res.clone().json().catch(() => null) as { code?: string, message?: string } | null
  if (!body) return res
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
  const statusMessage = code.toLowerCase()
  return new Response(JSON.stringify({ statusCode: res.status, statusMessage, message: message ?? body.message ?? statusMessage }), { status: res.status, headers })
}

/** Неудачный вход в журнал: неверный пароль или код 2FA. Отказ ограничителя (429) - не новая попытка. */
async function recordFailedLogin(path: string, request: Request | undefined, status: number, ip: string): Promise<void> {
  if (status < 400 || status >= 500 || status === 429) return
  let reason: string
  if (path === '/sign-in/username') {
    const body = await request?.json().catch(() => null) as { username?: unknown } | null | undefined
    const login = typeof body?.username === 'string' ? body.username.trim().slice(0, 64) : ''
    reason = login && `логин «${login}»`
  }
  else if (path === '/two-factor/verify-totp') reason = 'неверный код 2FA'
  else return
  await recordEvent({ type: 'login_failed', level: 'alarm', title: 'Неудачный вход в панель', detail: joinDetail(reason, ip) })
}

/** Запрос со страницы этой же панели: Origin (или Referer) того же хоста. */
function sameOrigin(event: H3Event): boolean {
  const from = getRequestHeader(event, 'origin') || getRequestHeader(event, 'referer')
  try {
    return !!from && new URL(from).host === getRequestHeader(event, 'host')
  }
  catch {
    return false
  }
}

export default defineEventHandler(async (event) => {
  const path = event.path.split('?')[0]!.replace(/^\/api\/auth/, '').replace(/\/+$/, '')
  if (!OPEN[event.method]?.has(path)) throw createError({ statusCode: 404, statusMessage: 'not_found' })
  if (event.method !== 'GET' && !sameOrigin(event)) {
    throw createError({ statusCode: 403, statusMessage: 'invalid_origin', message: 'Запрос не со страницы панели' })
  }
  const ip = clientAddress(event)
  const request = toWebRequest(event)
  const headers = new Headers(request.headers)
  headers.set('x-forwarded-for', ip)
  const forwarded = new Request(request, { headers })
  const copy = path === '/sign-in/username' ? forwarded.clone() : undefined
  const res = await useAuth().handler(forwarded)
  await recordFailedLogin(path, copy, res.status, ip)
  return res.status >= 400 ? humanError(res) : res
})
