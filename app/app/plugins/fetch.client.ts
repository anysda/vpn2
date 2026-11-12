// Обёртка над globalThis.fetch, через который идут все запросы ofetch:
// - текст ошибки из `message` тела отдаётся как `statusText`;
// - без cookie vpn2-signed-in запрос к закрытым ручкам сразу получает 401;
// - смена пароля и 2FA пересоздают сессию: остальные запросы ждут их ответа не дольше 15 с.
const OPEN = /^\/api\/(auth\/|bot\/|_nuxt_icon\/|sso$|version$)/
const ROTATES = /^\/api\/auth\/(two-factor\/(disable|verify-totp)|change-password)$/
const MARK = 'vpn2-signed-in'
const GONE = 'Сессия завершена, войдите заново'

function withText(res: Response, text: string) {
  Object.defineProperty(res, 'statusText', { value: text, configurable: true })
  return res
}

export default defineNuxtPlugin(() => {
  const base = globalThis.fetch
  let rotation: Promise<unknown> | null = null

  async function send(...args: Parameters<typeof fetch>) {
    const res = await base(...args)
    if (res.status < 400) return res
    const body = await res.clone().json().catch(() => null) as { message?: unknown, statusMessage?: unknown } | null
    const text = body?.message || body?.statusMessage
    return typeof text === 'string' && text ? withText(res, text) : res
  }

  globalThis.fetch = async (...args: Parameters<typeof fetch>) => {
    const url = new URL(args[0] instanceof Request ? args[0].url : String(args[0]), location.origin)
    const path = url.origin === location.origin && url.pathname.startsWith('/api/') ? url.pathname : null
    if (path === null) return base(...args)
    if (!ROTATES.test(path)) while (rotation) await rotation
    if (OPEN.test(path)) {
      const res = send(...args)
      if (ROTATES.test(path) && location.pathname !== '/login') {
        const done = Promise.race([res.catch(() => undefined), new Promise(resolve => setTimeout(resolve, 15_000))])
        rotation = done
        void done.then(() => { if (rotation === done) rotation = null })
      }
      if (path === '/api/auth/sign-out') void res.then(r => r.ok && (document.cookie = `${MARK}=; path=/; max-age=0`))
      return res
    }
    if (document.cookie.split(/;\s*/).some(c => c.startsWith(`${MARK}=`))) return send(...args)
    const body = JSON.stringify({ statusCode: 401, statusMessage: 'unauthorized', message: GONE })
    return withText(new Response(body, { status: 401, headers: { 'content-type': 'application/json' } }), GONE)
  }

  // Тост Nuxt UI ставит автозакрытие на паузу, пока окно без фокуса, и полоса
  // прогресса «зависает»: глушим эти два события окна раньше его слушателей.
  const swallow = (e: Event) => e.stopImmediatePropagation()
  window.addEventListener('blur', swallow)
  window.addEventListener('focus', swallow)
})
