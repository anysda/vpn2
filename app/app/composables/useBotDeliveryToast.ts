import { botDeliveryText, botQueueText, type BotDelivery } from '#shared/bot-delivery'

export function useBotDeliveryToast() {
  const rows = useState<BotDelivery[]>('bot-deliveries', () => [])
  const seen = useState<Record<string, string>>('bot-delivery-seen', () => ({}))
  const queued = computed(() => rows.value.filter(row => row.status === 'queued').length)
  const toast = useToast()
  const queueId = 'bot-queue'

  function updateQueue() {
    const current = toast.toasts.value.find(item => item.id === queueId)
    if (!queued.value) {
      if (current) toast.remove(queueId)
      return
    }
    const title = botQueueText(queued.value)
    if (current?.title === title) return
    const options = { title, color: 'warning' as const, duration: 0 }
    if (current) toast.update(queueId, options)
    else toast.add({ id: queueId, ...options })
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
