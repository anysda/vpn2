/**
 * Сервер кладёт в текст статуса латинский код ошибки (invalid_credentials,
 * route_exists, …), а текст для человека, если он есть, — в `message` тела;
 * без него `message` равен коду. Caddy перед панелью текст статуса заменяет
 * на стандартный («401 Unauthorized»), а по HTTP/2 текста нет вовсе, и
 * `err.statusMessage` во фронте показывал человеку «Unauthorized» или пустой тост.
 *
 * Берём текст из тела ответа и отдаём его через `statusText`: ofetch читает
 * `statusMessage` ошибки именно оттуда, и все места фронта видят его. У ошибки
 * валидации h3 в `message` дамп zod, ей оставляем код «Validation Error».
 */
export default defineNuxtPlugin(() => {
  globalThis.$fetch = globalThis.$fetch.create({
    onResponseError({ response }) {
      const data = response._data as { statusMessage?: unknown, message?: unknown } | undefined
      const text = (data?.statusMessage !== 'Validation Error' && data?.message) || data?.statusMessage
      if (typeof text === 'string' && text) {
        Object.defineProperty(response, 'statusText', { value: text, configurable: true })
      }
    },
  })
})
