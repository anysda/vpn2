/**
 * Lightweight reactive view of the Telegram bot config — used to disable
 * "send to Telegram" controls when the bot is not configured / not running.
 * Bots/Section.vue is the source of truth (writes via PUT); other components
 * just read.
 */
interface TgStatus {
  configured: boolean
  running: boolean
  bot_token_masked: string
  chat_id: string | number | ''
}

export function useTelegramStatus() {
  const { data, refresh } = useFetch<TgStatus>('/api/admin/telegram', {
    key: 'tg-status',
    default: () => ({ configured: false, running: false, bot_token_masked: '', chat_id: '' }),
    server: false,
  })

  const canSend = computed(() => Boolean(data.value?.configured && data.value?.running))
  const reason = computed(() => {
    if (!data.value?.configured) return 'Telegram-бот не настроен'
    if (!data.value?.running) return 'Telegram-бот не запущен'
    return ''
  })

  let timer: ReturnType<typeof setInterval> | null = null
  onMounted(() => {
    if (!timer) timer = setInterval(() => { void refresh() }, 15_000)
  })
  onUnmounted(() => { if (timer) { clearInterval(timer); timer = null } })

  useVisibleRefresh(refresh)

  return { status: data, canSend, reason, refresh }
}
