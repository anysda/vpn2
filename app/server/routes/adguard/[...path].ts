import type { H3Event } from 'h3'
import { useAuth } from '../../utils/auth'

/**
 * Веб-интерфейс AdGuard Home под /adguard/. Пускаем по сессии панели (любой
 * способ входа), учётку AdGuard подставляем сами — своей формы входа AdGuard
 * человек не видит, пароль в браузер не уходит. Сам AdGuard слушает только
 * 127.0.0.1.
 */
const PREFIX = '/adguard'
const DROP = new Set(['cookie', 'authorization', 'origin', 'referer'])

function basicAuth(): string | null {
  const cfg = useRuntimeConfig()
  const user = String(cfg.aghUser ?? '')
  const pass = String(cfg.aghPassword ?? '')
  return user && pass ? `Basic ${Buffer.from(`${user}:${pass}`).toString('base64')}` : null
}

/** Ответный Location от AdGuard указывает в корень — возвращаем его под подпуть. */
function rewriteLocation(location: string, upstream: string): string {
  const local = location.startsWith(upstream) ? location.slice(upstream.length) || '/' : location
  return local.startsWith('/') && !local.startsWith('//') ? PREFIX + local : local
}

function wantsPage(event: H3Event): boolean {
  return event.method === 'GET' && (getRequestHeader(event, 'accept') ?? '').includes('text/html')
}

export default defineEventHandler(async (event) => {
  const session = await useAuth().api.getSession({ headers: event.headers })
  if (!session) {
    if (wantsPage(event)) return sendRedirect(event, '/login')
    // Не createError: вне /api Nuxt рисует ошибку страницей и уводит на вход,
    // а запросам интерфейса AdGuard нужен честный 401.
    setResponseStatus(event, 401, 'Unauthorized')
    return 'Нужно войти'
  }

  const url = getRequestURL(event)
  const rest = url.pathname.slice(PREFIX.length)
  if (!rest) return sendRedirect(event, `${PREFIX}/${url.search}`)

  const auth = basicAuth()
  if (!auth) throw createError({ statusCode: 503, statusMessage: 'AdGuard не настроен' })

  const upstream = String(useRuntimeConfig().aghUrl).replace(/\/+$/, '')
  // Cookie и адрес панели AdGuard'у не нужны; proxyRequest передал бы их как есть.
  const headers = new Headers()
  for (const [name, value] of Object.entries(getProxyRequestHeaders(event))) {
    if (!DROP.has(name) && typeof value === 'string') headers.set(name, value)
  }
  headers.set('authorization', auth)
  const raw = ['GET', 'HEAD'].includes(event.method) ? undefined : await readRawBody(event, false)
  const body = raw ? new Uint8Array(raw) : undefined

  return sendProxy(event, upstream + rest + url.search, {
    fetchOptions: { method: event.method, headers, body, redirect: 'manual' },
    cookiePathRewrite: `${PREFIX}/`,
    onResponse(event, response) {
      const location = response.headers.get('location')
      if (location) setResponseHeader(event, 'location', rewriteLocation(location, upstream))
    },
  })
})
