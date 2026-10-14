<script setup lang="ts">
import { botDeliveryText, type BotDelivery } from '#shared/bot-delivery'

// Рассылка от бота всем клиентам с ботом. Без настроенного бота отправить
// нечем: кнопка выключена, рядом подсказка.
const { status } = useTelegramStatus()
const { show: showDelivery } = useBotDeliveryToast()
const toast = useToast()

const message = ref('')
const sending = ref(false)
const configured = computed(() => Boolean(status.value?.configured))
// имя бота из getMe; пока бот не отвечает, его нет
const bot = computed(() => status.value?.username || '')

async function send() {
  const text = message.value.trim()
  if (!text || !configured.value) return
  sending.value = true
  try {
    const res = await $fetch<{ recipients: number, delivery: BotDelivery | null }>('/api/admin/broadcast', { method: 'POST', body: { text } })
    if (res.delivery?.status === 'accepted') toast.add({ title: botDeliveryText(res.delivery), color: 'success' })
    else if (res.delivery) showDelivery(res.delivery)
    else toast.add({ title: 'Нет получателей рассылки.', color: 'warning' })
    message.value = ''
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: 'Ошибка', color: 'error', description: err.statusMessage })
  }
  finally {
    sending.value = false
  }
}
</script>

<template>
  <div class="card sc bc ov-broadcast">
    <div class="sh"><h3>Рассылка от бота</h3><span v-if="bot" class="tag">@{{ bot }}</span></div>
    <textarea v-model="message" maxlength="4096" placeholder="Текст сообщения всем клиентам с ботом" aria-label="Сообщение юзерам" />
    <div class="arow">
      <span v-if="!configured" class="hint">Сначала настройте Telegram-бота</span>
      <button
        type="button" class="pill w" style="margin-left:auto"
        :disabled="!configured || sending || !message.trim()" @click="send"
      >Отправить</button>
    </div>
  </div>
</template>
