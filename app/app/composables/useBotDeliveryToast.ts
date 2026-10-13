import { botDeliveryText, botQueueText, type BotDelivery } from '#shared/bot-delivery'

export function useBotDeliveryToast() {
  const rows = useState<BotDelivery[]>('bot-deliveries', () => [])
  const seen = useState<Record<string, string>>('bot-delivery-seen', () => ({}))
  const queueShown = useState('bot-queue-shown', () => false)
  const queued = computed(() => rows.value.filter(row => row.status === 'queued').length)
  const toast = useToast()
  const queueId = 'bot-queue'

  function updateQueue() {
    const current = toast.toasts.value.find(item => item.id === queueId)
    if (!queued.value) {
      if (current) toast.remove(queueId)
      return
    }
    if (!import.meta.client || queueShown.value) return
    // sessionStorage переживает перезагрузку, но принадлежит одной вкладке.
    // Если хранилище запрещено, useState предотвращает повторы до перезагрузки.
    try {
      queueShown.value = sessionStorage.getItem('bot-queue-shown') === '1'
      if (queueShown.value) return
      sessionStorage.setItem('bot-queue-shown', '1')
    }
    catch { /* Сохраняем показ в памяти, даже если хранилище недоступно. */ }
    queueShown.value = true
    toast.add({
      id: queueId,
      title: botQueueText(queued.value),
      description: 'Подробности — в «Уведомлениях».',
      color: 'warning',
      duration: 5000,
      ui: { root: 'bot-queue-toast' },
    })
  }

  function sync(deliveries: BotDelivery[], announce = true) {
    rows.value = deliveries
    for (const row of deliveries) {
      const previous = seen.value[row.id]
      seen.value[row.id] = row.status
      // Успех очереди убирает общий тост. Окончательная ошибка остаётся
      // отдельной и не считается ожидающим уведомлением.
      if (announce && row.status === 'failed' && previous !== 'failed') {
        toast.add({ id: `bot-${row.id}`, title: botDeliveryText(row), color: 'error' })
      }
    }
    updateQueue()
  }

  function show(row: BotDelivery) {
    sync([...rows.value.filter(item => item.id !== row.id), row])
  }

  return { rows, queued, sync, show }
}
