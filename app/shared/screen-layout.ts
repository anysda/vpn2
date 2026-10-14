/**
 * Раскладка главной: какие блоки на обзоре и в каком порядке (колонка одна, right пуст).
 * Хранится у пользователя (users.screen_layout), правится в «Настроить экран».
 */
export const SCREEN_MODULES = {
  traffic: { label: 'Трафик, выходы и DNS', hint: 'волна за сутки, два кольца', column: 'left' },
  nodes: { label: 'Узлы', hint: 'Entry и экзиты с метриками', column: 'left' },
  activity: { label: 'Активность и клиенты', hint: 'сетка по дням, онлайн и офлайн', column: 'left' },
  events: { label: 'Рассылка и события', hint: 'сообщение от бота, лента', column: 'left' },
} as const

export type ScreenModuleId = keyof typeof SCREEN_MODULES

export const SCREEN_MODULE_IDS = Object.keys(SCREEN_MODULES) as ScreenModuleId[]

export interface ScreenLayout {
  left: ScreenModuleId[]
  right: ScreenModuleId[]
  // Убранные с экрана. Хранятся явно: модуль, которого нет нигде, считается
  // новым и встаёт в свою колонку по умолчанию.
  hidden: ScreenModuleId[]
}

export function defaultScreenLayout(): ScreenLayout {
  const byColumn = (c: 'left' | 'right') => SCREEN_MODULE_IDS.filter(id => SCREEN_MODULES[id].column === c)
  return { left: byColumn('left'), right: byColumn('right'), hidden: [] }
}

/** Сохранённая раскладка (или null) в полную: без чужих id и дублей, новые модули в конец своей колонки. */
export function normalizeScreenLayout(saved: Partial<Record<keyof ScreenLayout, unknown>> | null | undefined): ScreenLayout {
  if (!saved) return defaultScreenLayout()
  const seen = new Set<ScreenModuleId>()
  const pick = (list: unknown) => (Array.isArray(list) ? list : []).filter((id): id is ScreenModuleId => {
    if (typeof id !== 'string' || !Object.hasOwn(SCREEN_MODULES, id) || seen.has(id as ScreenModuleId)) return false
    seen.add(id as ScreenModuleId)
    return true
  })
  const out: ScreenLayout = { left: pick(saved.left), right: pick(saved.right), hidden: pick(saved.hidden) }
  for (const id of SCREEN_MODULE_IDS) {
    if (!seen.has(id)) out[SCREEN_MODULES[id].column].push(id)
  }
  return out
}
