/** Имя → безопасный для файла слаг: нижний регистр, не-буквы/цифры → «-». */
export function slugify(s: string): string {
  return s.trim().toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-+|-+$/g, '') || 'x'
}

/**
 * Идентичность конфига девайса — «<клиент>-<девайс>». Используется для имён
 * файлов конфигов (.conf / .ovpn) и подписей при отправке.
 */
export function deviceConfigName(clientName: string, deviceName: string): string {
  return `${slugify(clientName)}-${slugify(deviceName)}`
}
