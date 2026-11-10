import { POLL_TIMEOUT_MS } from '~/composables/useClients'

/**
 * Единственное место, где панель читает `/api/admin/telegram`: и раздел
 * настроек бота (Bots/Section.vue, он же пишет через PUT), и кнопки
 * «отправить в Telegram», которым нужно знать, настроен ли бот и запущен ли.
 */
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

/** `pollMs` - шаг опроса; у одного ключа таймер общий, шаг задаёт первый смонтированный. */
export function useTelegramStatus(pollMs = 15_000) {
  // Как список клиентов: сбой опроса оставляет последний удачный статус, а не
  // мигает в «не настроен»; зависший запрос не держит опрос.
  const last = useState<TgStatus>('tg-status-last', () => ({
    configured: false, running: false, bot_token_masked: '', chat_id: '', admin_username: '',
    username: '', clients_total: 0, clients_subscribed: 0, queued: 0, delivered_day: 0,
  }))
  const { data, refresh } = useFetch<TgStatus>('/api/admin/telegram', {
    key: 'tg-status',
    default: () => last.value,
    transform: status => (last.value = status),
    server: false,
    dedupe: 'defer',
    timeout: POLL_TIMEOUT_MS,
  })

  const canSend = computed(() => Boolean(data.value?.configured && data.value?.running))
  const reason = computed(() => {
    if (!data.value?.configured) return 'Telegram-бот не настроен'
    if (!data.value?.running) return 'Telegram-бот не запущен'
    return ''
  })

  useSharedPoll('tg-status', pollMs)

  useVisibleRefresh(refresh)

  return { status: data, canSend, reason, refresh }
}
