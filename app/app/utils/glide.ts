/**
 * Скользящая подложка ряда кнопок (.glide) встаёт под выбранную (.on).
 * Первый показ, jump и «меньше движения» - без анимации. false - ставить некуда.
 */
export function placeGlide(box: HTMLElement | undefined, placed: boolean, jump = false): boolean {
  const g = box?.querySelector<HTMLElement>(':scope>.glide')
  const on = box?.querySelector<HTMLElement>(':scope>.on')
  if (!box || !g || !on || !box.offsetWidth) return false
  jump ||= !placed || reducedMotion()
  g.classList.toggle('now', jump)
  const r = on.getBoundingClientRect(), q = box.getBoundingClientRect()
  g.style.height = `${r.height}px`
  g.style.width = `${r.width}px`
  g.style.transform = `translate(${r.left - q.left - box.clientLeft + box.scrollLeft}px,${r.top - q.top - box.clientTop + box.scrollTop}px)`
  if (jump) requestAnimationFrame(() => g.classList.remove('now'))
  return true
}
