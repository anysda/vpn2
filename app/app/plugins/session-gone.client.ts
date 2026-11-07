/**
 * Без сессии запросы панели не отправляем. Cookie сессии httpOnly, и страница
 * не знает, что её уже нет: стёрли, истекла, вышли в другой вкладке. Опрос,
 * ушедший в такой момент, получал 401, а браузер писал ошибку в консоль.
 * Сервер держит читаемый признак входа (server/middleware/session-mark.ts):
 * признака нет — запрос не уходит, странице сразу отдаётся тот же 401.
 * Открытые ручки (вход, кнопка SSO, версия, иконки) уходят всегда: по ним
 * сервер и ставит признак после входа.
 *
 * Граница: признак проверяется перед отправкой. Cookie, стёртая между
 * проверкой и отправкой, или сессия, отозванная на сервере, дают 401, как раньше.
 * Оборачиваем `fetch`, как session-rotation: через него идут `$fetch` и `useFetch`.
 */
const MARK = 'vpn2-signed-in'
const OPEN = /^\/api\/(auth\/|bot\/|_nuxt_icon\/|sso$|version$)/
const GONE = 'Сессия завершена, войдите заново'

function apiPath(input: RequestInfo | URL) {
  const raw = input instanceof Request ? input.url : String(input)
  const url = new URL(raw, location.origin)
  return url.origin === location.origin && url.pathname.startsWith('/api/') ? url.pathname : null
}

const signedIn = () => document.cookie.split(/;\s*/).some(c => c.startsWith(`${MARK}=`))

export default defineNuxtPlugin(() => {
  const baseFetch = globalThis.fetch
  globalThis.fetch = async (...args: Parameters<typeof fetch>) => {
    const path = apiPath(args[0])
    if (path === null) return baseFetch(...args)
    if (OPEN.test(path)) {
      const res = await baseFetch(...args)
      // Выход: признак снимаем сразу, опрос не ждёт следующего ответа сервера.
      if (path === '/api/auth/sign-out' && res.ok) document.cookie = `${MARK}=; path=/; max-age=0`
      return res
    }
    if (signedIn()) return baseFetch(...args)
    const res = new Response(JSON.stringify({ statusCode: 401, statusMessage: 'unauthorized', message: GONE }), {
      status: 401,
      headers: { 'content-type': 'application/json' },
    })
    // Как fetch-error-text: ofetch берёт текст ошибки из statusText.
    Object.defineProperty(res, 'statusText', { value: GONE, configurable: true })
    return res
  }
})
