/**
 * Выключение и включение 2FA и смена пароля пересоздают сессию: сервер ставит
 * новую cookie и сразу удаляет старую. Запрос панели, ушедший со старой cookie,
 * пока такой запрос в пути, доходит до сервера после удаления и получает 401.
 * Пока запрос, пересоздающий сессию, в пути, остальные запросы к `/api/*` ждут
 * его конца и уходят уже с новой cookie.
 *
 * Оборачиваем `fetch`, как fetch-error-text: и `$fetch`, и `useFetch` идут
 * через ofetch, а он берёт `fetch` из `globalThis` на каждый запрос.
 */
const GATE_MS = 15_000
const ROTATES = /^\/api\/auth\/(two-factor\/(disable|verify-totp)|change-password)$/

function apiPath(input: RequestInfo | URL) {
  const raw = input instanceof Request ? input.url : String(input)
  const url = new URL(raw, location.origin)
  return url.origin === location.origin && url.pathname.startsWith('/api/') ? url.pathname : null
}

export default defineNuxtPlugin(() => {
  const baseFetch = globalThis.fetch
  let rotation: Promise<unknown> | null = null
  globalThis.fetch = async (...args: Parameters<typeof fetch>) => {
    const path = apiPath(args[0])
    if (path === null) return baseFetch(...args)
    // verify-totp на странице входа сессии ещё нет: ждать некому.
    if (ROTATES.test(path) && location.pathname !== '/login') {
      const req = baseFetch(...args)
      // Зависший без ответа запрос держит остальные не дольше предела.
      const done = Promise.race([req.catch(() => undefined), new Promise(resolve => setTimeout(resolve, GATE_MS))])
      rotation = done
      void done.then(() => { if (rotation === done) rotation = null })
      return req
    }
    while (rotation) await rotation
    return baseFetch(...args)
  }
})
