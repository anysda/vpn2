/**
 * Копирование текста в буфер обмена с фолбэком для небезопасного контекста.
 *
 * `navigator.clipboard` доступен только в secure context (HTTPS / localhost).
 * Панель на стенде отдаётся по HTTP на IP — там Clipboard API отсутствует,
 * остаётся legacy `execCommand('copy')`.
 *
 * Нюанс: модалки панели — reka-ui Dialog с focus-trap. Если смонтировать
 * textarea вне диалога, focus-trap синхронно вернёт фокус в модалку на
 * `focusin`, textarea останется без фокуса, copy ничего не скопирует — но
 * `execCommand` всё равно вернёт true (отсюда ложное «скопировано»). Поэтому
 * textarea монтируется ВНУТРЬ диалога, и перед вердиктом проверяется, что
 * фокус реально на ней — иначе честно возвращаем false.
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

  // Legacy execCommand. Монтируем в активный диалог (или в любой открытый),
  // иначе focus-trap не отдаст фокус textarea.
  const active = document.activeElement as HTMLElement | null
  const host = (active?.closest('[role="dialog"]') as HTMLElement | null)
    ?? (document.querySelector('[role="dialog"]') as HTMLElement | null)
    ?? document.body

  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  // Off-screen, но отрендеренная: display:none / visibility:hidden ломают copy.
  ta.style.cssText = 'position:fixed;bottom:0;left:0;width:1px;height:1px;'
    + 'padding:0;border:0;margin:0;opacity:0;'
  host.appendChild(ta)

  let ok = false
  try {
    ta.focus({ preventScroll: true })
    ta.select()
    ta.setSelectionRange(0, text.length)
    // execCommand копирует выделение textarea только пока она в фокусе. Если
    // focus-trap фокус увёл — copy пустой; не врём про успех, возвращаем false.
    ok = document.activeElement === ta && document.execCommand('copy')
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
