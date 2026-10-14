/** Разделы в шапке, по порядку: от него зависит, с какой стороны въезжает страница. */
export const TAB_PAGES = [
  { path: '/', label: 'Обзор' },
  { path: '/clients', label: 'Клиенты' },
  { path: '/routes', label: 'Маршруты' },
  { path: '/dns', label: 'DNS' },
  { path: '/settings', label: 'Настройки' },
] as const

/** Направление перехода между разделами; null — переход не между разделами шапки. */
export function tabDirection(from: string, to: string): 'fwd' | 'back' | null {
  const a = TAB_PAGES.findIndex(t => t.path === from)
  const b = TAB_PAGES.findIndex(t => t.path === to)
  if (a < 0 || b < 0 || a === b) return null
  return b > a ? 'fwd' : 'back'
}
