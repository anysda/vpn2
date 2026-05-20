/**
 * Копирование текста в буфер обмена с фолбэком для небезопасного контекста.
 *
 * `navigator.clipboard` доступен только в secure context (HTTPS или localhost).
 * Панель на стенде отдаётся по HTTP на IP — там Clipboard API отсутствует,
 * и прямой вызов `navigator.clipboard.writeText` молча падал. Фолбэк через
 * скрытый textarea + `execCommand('copy')` работает и по HTTP.
 *
 * @returns true — скопировано, false — не удалось (вызывающий показывает ошибку).
 */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false

  // Быстрый путь — современный Clipboard API (secure context).
  if (import.meta.client && window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    }
    catch {
      // упадём в legacy-фолбэк ниже
    }
  }

  // Фолбэк для HTTP: скрытый textarea + execCommand.
  if (import.meta.client) {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.setAttribute('readonly', '')
      ta.style.position = 'fixed'
      ta.style.top = '0'
      ta.style.left = '-9999px'
      document.body.appendChild(ta)
      ta.select()
      ta.setSelectionRange(0, text.length)
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      return ok
    }
    catch {
      return false
    }
  }

  return false
}
