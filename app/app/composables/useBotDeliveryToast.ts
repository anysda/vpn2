import { botDeliveryText, type BotDelivery } from '#shared/bot-delivery'

export function useBotDeliveryToast() {
  const seen = useState<Record<string, string>>('bot-delivery-seen', () => ({}))
  const toast = useToast()
  return (row: BotDelivery, announce = true) => {
    const version = `${row.status}:${row.remaining}`
    if (seen.value[row.id] === version) return
    const previous = seen.value[row.id]
    seen.value[row.id] = version
    if (!announce) return
    // При открытии страницы не повторяем всю историю успешных передач.
    if (row.status === 'accepted' && previous === undefined) return
    toast.add({ id: `bot-${row.id}`, title: botDeliveryText(row), color: row.status === 'accepted' ? 'success' : row.status === 'failed' ? 'error' : 'warning' })
  }
}
