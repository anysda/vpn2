/**
 * Признак входа, который видит страница. Cookie сессии better-auth — httpOnly:
 * страница не знает, что её уже нет (стёрли, истекла, вышли в другой вкладке),
 * и опрос, ушедший в такой момент, получал 401 (app/plugins/fetch.client.ts).
 * Признак повторяет наличие cookie сессии в запросе: пришла — ставим, нет —
 * снимаем. Он ничего не разрешает: сессию по-прежнему проверяет сервер.
 * Без срока: признак живёт не меньше cookie сессии, иначе страница сочла бы
 * себя вышедшей при живой сессии.
 */
const MARK = 'vpn2-signed-in'
// С secure-cookie better-auth добавляет к имени приставку __Secure-.
const SESSION = /(^|-)better-auth\.session_token$/

export default defineEventHandler((event) => {
  const cookies = parseCookies(event)
  const session = Object.keys(cookies).some(name => SESSION.test(name))
  if (session === (MARK in cookies)) return
  const opts = { path: '/', sameSite: 'lax', secure: !!useRuntimeConfig().session.cookie.secure } as const
  if (session) setCookie(event, MARK, '1', { ...opts, httpOnly: false })
  else deleteCookie(event, MARK, opts)
})
