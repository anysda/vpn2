/**
 * Сервер кладёт в текст статуса латинский код ошибки (invalid_credentials,
 * route_exists, …), а текст для человека, если он есть, — в `message` тела;
 * без него `message` равен коду. Caddy перед панелью текст статуса заменяет
 * на стандартный («401 Unauthorized»), а по HTTP/2 текста нет вовсе, и
 * `err.statusMessage` во фронте показывал человеку «Unauthorized» или пустой тост.
 *
 * Берём текст из тела ответа и отдаём его через `statusText`: ofetch читает
 * `statusMessage` ошибки именно оттуда, и все места фронта видят его.
 * Запросы с `responseType: 'text'` (.conf, .ovpn) получают тело ошибки
 * строкой — разбираем её сами.
 */
export default defineNuxtPlugin(() => {
  globalThis.$fetch = globalThis.$fetch.create({
    onResponseError({ response }) {
      let raw: unknown = response._data
      if (typeof raw === 'string') {
        try { raw = JSON.parse(raw) }
        catch { return }
      }
      const data = raw as { statusMessage?: unknown, message?: unknown } | null | undefined
      const text = data?.message || data?.statusMessage
      if (typeof text === 'string' && text) {
        Object.defineProperty(response, 'statusText', { value: text, configurable: true })
      }
    },
  })
})
