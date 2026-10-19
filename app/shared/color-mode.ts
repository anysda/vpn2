// Настройки темы одни на обе панели: основную ставит модуль @nuxtjs/color-mode
// (nuxt.config.ts), аварийную - тот же скрипт модуля в сборке emergency/vite.config.ts.
// Выбор «как в системе» скрипт сводит к light или dark в data-theme до отрисовки.
export const colorMode = {
  preference: 'system',
  fallback: 'dark',
  dataValue: 'theme',
  storage: 'localStorage',
  storageKey: 'nuxt-color-mode',
  globalName: '__NUXT_COLOR_MODE__',
  classPrefix: '',
  classSuffix: '',
} as const
