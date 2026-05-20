/**
 * Копирование текста в буфер обмена с фолбэком для небезопасного контекста.
 *
 * `navigator.clipboard` доступен только в secure context (HTTPS или localhost).
 * Панель на стенде отдаётся по HTTP на IP — там Clipboard API отсутствует.
 * Фолбэк: скрытый textarea + `execCommand('copy')`. Чтобы execCommand реально
 * скопировал (а не вернул true вхолостую), textarea должен быть в DOM,
 * сфокусирован и с выделенным текстом — поэтому `focus()` обязателен, а позиция
 * не уносится далеко за экран (иначе фокус не встаёт).
 *
 * @returns true — текст реально скопирован; false — не удалось (показать ошибку).
 */
export async function copyText(text: string): Promise<boolean> {
  if (!text || !import.meta.client) return false

  // Secure context — современный Clipboard API.
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    }
    catch {
      // упадём в legacy-фолбэк ниже
    }
  }

  // HTTP / без secure context — legacy execCommand.
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.cssText
    = 'position:fixed;top:0;left:0;width:2em;height:2em;padding:0;'
    + 'border:none;outline:none;box-shadow:none;background:transparent;'
    + 'opacity:0;z-index:-1;'
  document.body.appendChild(ta)

  let ok = false
  try {
    ta.focus()
    ta.select()
    ta.setSelectionRange(0, text.length)
    ok = document.execCommand('copy')
  }
  catch {
    ok = false
  }
  finally {
    ta.remove()
  }
  return ok
}
