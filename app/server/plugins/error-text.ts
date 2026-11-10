/**
 * Текст для человека по коду ошибки. Обработчики бросают
 * `createError({ statusMessage: 'not_found' })`, и без текста `message` равен
 * коду — его видел человек в тосте и в Telegram. Здесь код остаётся в
 * `statusMessage`, а в `message` встаёт текст; заданный обработчиком текст
 * не трогаем. Тексты лежат в `app/shared/error-text.json`.
 */
import { panelErrorText } from '../../shared/error-text'

export default defineNitroPlugin((nitroApp) => {
  // Хук синхронный: Nitro зовёт его перед errorHandler и не ждёт, так что
  // текст успевает в ответ.
  nitroApp.hooks.hook('error', (error, { event }) => {
    if (!event || !isError(error)) return
    if (error.message && error.message !== error.statusMessage) return
    const text = panelErrorText(error.statusMessage ?? '')
    if (text) error.message = text
  })
})
