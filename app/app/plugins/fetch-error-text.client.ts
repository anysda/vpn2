/**
 * Сервер кладёт в текст статуса латинский код ошибки (invalid_credentials,
 * route_exists, …), а текст для человека, если он есть, — в `message` тела;
 * без него `message` равен коду. Caddy перед панелью текст статуса заменяет
 * на стандартный («401 Unauthorized»), а по HTTP/2 текста нет вовсе, и
 * `err.statusMessage` во фронте показывал человеку «Unauthorized» или пустой тост.
 *
 * Берём текст из тела ответа и отдаём его через `statusText`: ofetch читает
 * `statusMessage` ошибки именно оттуда, и все места фронта видят его.
 * Оборачиваем `fetch`, а не `$fetch`: Nuxt запоминает `$fetch` при импорте,
 * и подмена `globalThis.$fetch` из плагина до него не доходит, а `fetch`
 * ofetch берёт из `globalThis` на каждый запрос.
 */
export default defineNuxtPlugin(() => {
  const baseFetch = globalThis.fetch
  globalThis.fetch = async (...args: Parameters<typeof fetch>) => {
    const res = await baseFetch(...args)
    if (res.ok || res.status < 400) return res
    let text: unknown
    try {
      const data = JSON.parse(await res.clone().text()) as { statusMessage?: unknown, message?: unknown } | null
      text = data?.message || data?.statusMessage
    }
    catch {
      return res
    }
    if (typeof text === 'string' && text) {
      Object.defineProperty(res, 'statusText', { value: text, configurable: true })
    }
    return res
  }
})
