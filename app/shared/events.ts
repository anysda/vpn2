/** Запись ленты «События» (GET /api/events). */
export type EventLevel = 'alarm' | 'warn' | 'info' | 'ok'

export interface PanelEvent {
  id: number
  // мс от эпохи
  at: number
  type: string
  level: EventLevel
  title: string
  detail: string
}
