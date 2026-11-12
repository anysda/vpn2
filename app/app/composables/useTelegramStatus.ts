interface TgStatus {
  configured: boolean
  running: boolean
  bot_token_masked: string
  chat_id: string | number | ''
  admin_username: string
  username: string
  clients_total: number
  clients_subscribed: number
  queued: number
  delivered_day: number
}

/** Статус бота; сбой опроса оставляет последний удачный, таймаут дольше ворот смены сессии (15 с). */
export function useTelegramStatus(pollMs = 15_000) {
  const last = useState<TgStatus | null>('tg-status-last', () => null)
  const { data, refresh } = useFetch<TgStatus | null>('/api/admin/telegram', {
    key: 'tg-status',
    default: () => last.value,
    transform: s => (last.value = s),
    server: false,
    dedupe: 'defer',
    timeout: 20_000,
  })
  usePoll(refresh, pollMs)
  const reason = computed(() => !data.value?.configured ? 'Telegram-бот не настроен' : !data.value.running ? 'Telegram-бот не запущен' : '')
  return { status: data, canSend: computed(() => !reason.value), reason, refresh }
}
