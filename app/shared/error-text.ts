import table from './error-text.json' with { type: 'json' }

// Единая таблица текстов ошибок: `panel` читает панель (плагин error-text),
// `bot` и `plural` читает бот.
export function panelErrorText(code: string): string | undefined {
  return Object.hasOwn(table.panel, code) ? (table.panel as Record<string, string>)[code] : undefined
}
