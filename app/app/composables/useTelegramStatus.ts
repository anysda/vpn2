/** Статус бота; таймаут дольше ворот смены сессии (15 с). */
export function useTelegramStatus(pollMs = 15_000) {
  const { data, refresh } = usePolledFetch('/api/admin/telegram', pollMs, 20_000, null as Api<'/api/admin/telegram'> | null)
  const reason = computed(() => !data.value?.configured ? 'Telegram-бот не настроен' : !data.value.running ? 'Telegram-бот не запущен' : '')
  return { status: data, canSend: computed(() => !reason.value), reason, refresh }
}
