/**
 * Текст для человека по коду ошибки. Обработчики бросают
 * `createError({ statusMessage: 'not_found' })`, и без текста `message` равен
 * коду — его видел человек в тосте и в Telegram. Здесь код остаётся в
 * `statusMessage`, а в `message` встаёт текст; заданный обработчиком текст
 * не трогаем.
 */
const TEXT: Record<string, string> = {
  Unauthorized: 'Нужно войти',
  unauthorized: 'Неверный секрет бота',
  not_found: 'Не найдено',
  invalid_id: 'Неверный идентификатор',
  invalid_params: 'Неверные параметры запроса',
  invalid_chat_id: 'Неверный идентификатор чата Telegram',
  invalid_password: 'Неверный пароль клиента',
  not_linked: 'Telegram не привязан ни к одному клиенту',
  loopback_only: 'API бота доступен только с этой ноды',
  tgbot_secret_not_configured: 'Не задан секрет Telegram-бота в конфиге',
  wg_public_host_not_configured: 'Не задан публичный адрес WireGuard в конфиге',
  ovpn_public_host_not_configured: 'Не задан публичный адрес OpenVPN в конфиге',
  entry_host_not_configured: 'Не задан публичный адрес сервера в конфиге',
  totp_already_enabled: '2FA уже включена',
  totp_not_initiated: 'Настройка 2FA не начата, начните заново',
  invalid_totp: 'Код не подошёл',
  session_revoked: 'Сессия завершена, войдите заново',
  insert_failed: 'Не удалось сохранить запись',
}

export default defineNitroPlugin((nitroApp) => {
  // Хук синхронный: Nitro зовёт его перед errorHandler и не ждёт, так что
  // текст успевает в ответ.
  nitroApp.hooks.hook('error', (error, { event }) => {
    if (!event || !isError(error)) return
    if (error.message && error.message !== error.statusMessage) return
    const text = TEXT[error.statusMessage ?? '']
    if (text) error.message = text
  })
})
