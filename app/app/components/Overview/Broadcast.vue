<script setup lang="ts">
import { botDeliveryText } from '#shared/bot-delivery'

const { status } = useTelegramStatus()
const delivery = useBotDelivery()
const { busy, act } = useAct()
const message = ref('')
const configured = computed(() => Boolean(status.value?.configured))

function send() {
  const text = message.value.trim()
  if (!text || !configured.value) return
  return act('send', async () => {
    const res = await $fetch('/api/admin/broadcast', { method: 'POST', body: { text } })
    message.value = ''
    return res.delivery
  }, (d) => {
    if (!d) return { title: 'Нет получателей рассылки.', color: 'warning' }
    if (d.status === 'accepted') return { title: botDeliveryText(d), color: 'success' }
    delivery.show(d)
  }, { title: 'Ошибка' })
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
      <button type="button" class="pill w ml-auto" :disabled="!configured || busy.has('send') || !message.trim()" @click="send">
        Отправить
      </button>
    </div>
  </div>
</template>
