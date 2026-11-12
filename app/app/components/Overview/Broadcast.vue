<script setup lang="ts">
import { botDeliveryText, type BotDelivery } from '#shared/bot-delivery'

const { status } = useTelegramStatus()
const delivery = useBotDelivery()
const toast = useToast()
const message = ref('')
const sending = ref(false)
const configured = computed(() => Boolean(status.value?.configured))

async function send() {
  const text = message.value.trim()
  if (!text || !configured.value) return
  sending.value = true
  try {
    const res = await $fetch<{ recipients: number, delivery: BotDelivery | null }>('/api/admin/broadcast', { method: 'POST', body: { text } })
    if (res.delivery?.status === 'accepted') toast.add({ title: botDeliveryText(res.delivery), color: 'success' })
    else if (res.delivery) delivery.show(res.delivery)
    else toast.add({ title: 'Нет получателей рассылки.', color: 'warning' })
    message.value = ''
  }
  catch (e) {
    toast.add({ title: 'Ошибка', color: 'error', description: errText(e, '') || undefined })
  }
  finally {
    sending.value = false
  }
}
</script>

<template>
  <div class="card sc bc flex min-h-[270px] flex-col max-[720px]:min-h-0">
    <div class="sh">
      <h3>Рассылка от бота</h3><span v-if="status?.username" class="tag">@{{ status.username }}</span>
    </div>
    <textarea v-model="message" class="area flex-1" maxlength="4096" placeholder="Текст сообщения всем клиентам с ботом" aria-label="Сообщение юзерам" />
    <div class="arow">
      <span v-if="!configured" class="hint">Сначала настройте Telegram-бота</span>
      <button type="button" class="pill w ml-auto" :disabled="!configured || sending || !message.trim()" @click="send">
        Отправить
      </button>
    </div>
  </div>
</template>
