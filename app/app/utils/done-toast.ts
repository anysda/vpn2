/**
 * Тост завершённой операции. Сервер вернул `warning` (изменение записано, но
 * уведомление в Telegram не ушло) - тот же тост цветом предупреждения с его текстом.
 */
export function doneToast(title: string, warning?: string) {
  return warning
    ? { title, description: warning, color: 'warning' as const }
    : { title, color: 'success' as const }
}
