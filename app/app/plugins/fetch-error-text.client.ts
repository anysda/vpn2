/**
 * Сервер кладёт код ошибки (invalid_credentials, invalid_value, …) и в текст
 * статуса, и в тело. Caddy перед панелью текст статуса заменяет на стандартный
 * («401 Unauthorized»), а по HTTP/2 текста нет вовсе, и `err.statusMessage`
 * во фронте показывал человеку «Unauthorized» или пустой тост.
 *
 * Берём код из тела ответа и отдаём его через `statusText`: ofetch читает
 * `statusMessage` ошибки именно оттуда, и все места фронта видят настоящий код.
 */
export default defineNuxtPlugin(() => {
  globalThis.$fetch = globalThis.$fetch.create({
    onResponseError({ response }) {
      const data = response._data as { statusMessage?: unknown, message?: unknown } | undefined
      const text = data?.statusMessage || data?.message
      if (typeof text === 'string' && text) {
        Object.defineProperty(response, 'statusText', { value: text, configurable: true })
      }
    },
  })
})
