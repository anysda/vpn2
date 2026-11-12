import { botDeliveryText, botQueueText, type BotDelivery } from '#shared/bot-delivery'

/** Строки колокольчика доставки бота: ошибка - красный тост, очередь - один жёлтый тост на вкладку. */
export function useBotDelivery() {
  const rows = useState<BotDelivery[]>('bot-deliveries', () => [])
  const seen = useState<Record<string, string>>('bot-delivery-seen', () => ({}))
  const queueShown = useState('bot-queue-shown', () => false)
  const queued = computed(() => rows.value.filter(r => r.status === 'queued').length)
  const toast = useToast()

  function sync(list: BotDelivery[], announce = true) {
    rows.value = list
    for (const r of list) {
      if (announce && r.status === 'failed' && seen.value[r.id] !== 'failed') toast.add({ id: `bot-${r.id}`, title: botDeliveryText(r), color: 'error' })
      seen.value[r.id] = r.status
    }
    if (!queued.value) return toast.remove('bot-queue')
    if (queueShown.value) return
    queueShown.value = true
    try {
      if (sessionStorage.getItem('bot-queue-shown')) return
      sessionStorage.setItem('bot-queue-shown', '1')
    }
    catch { /* хранилище запрещено: повторы держит useState до перезагрузки */ }
    toast.add({ id: 'bot-queue', title: botQueueText(queued.value), description: 'Подробности — в «Уведомлениях».', color: 'warning', duration: 5000, ui: { root: 'bot-queue-toast' } })
  }

  /** Ответ рассылки: строка сразу в колокольчике, не дожидаясь опроса. */
  const show = (row: BotDelivery) => sync([...rows.value.filter(r => r.id !== row.id), row])
  return { rows, queued, sync, show }
}
