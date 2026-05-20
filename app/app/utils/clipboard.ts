/**
 * Копирование текста в буфер обмена с фолбэком для небезопасного контекста.
 *
 * `navigator.clipboard` доступен только в secure context (HTTPS / localhost).
 * Панель на стенде отдаётся по HTTP на IP — там Clipboard API отсутствует.
 *
 * Фолбэк — скрытый textarea + `execCommand('copy')`. Важный нюанс: модалки
 * панели это reka-ui Dialog с focus-trap. Если смонтировать textarea в
 * `document.body` (вне диалога), focus-trap тут же вернёт фокус в модалку,
 * textarea останется без фокуса и copy молча проваливается (а execCommand
 * всё равно возвращает true — отсюда ложное «скопировано»). Поэтому textarea
 * монтируется ВНУТРЬ ближайшего `[role="dialog"]`.
 *
 * @returns true — текст реально скопирован; false — не удалось.
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

  // Legacy execCommand. Монтируем в ту же модалку, что и активный элемент,
  // иначе focus-trap диалога не отдаст фокус textarea.
  const active = document.activeElement as HTMLElement | null
  const host = (active?.closest('[role="dialog"]') as HTMLElement | null) ?? document.body

  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;'
    + 'opacity:0;border:0;padding:0;margin:0;'
  host.appendChild(ta)

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
    active?.focus?.()
  }
  return ok
}
