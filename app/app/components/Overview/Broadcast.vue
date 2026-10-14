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

<style scoped>
/* Свои стили: карточка стоит на странице клиентов, обёртки .ov там нет. */
.bc{padding:20px 22px;display:flex;flex-direction:column;min-height:270px}
.sh{display:flex;justify-content:space-between;align-items:center;gap:12px}
.sh h3{color:var(--t1);font-size:15px}
.tag{display:inline-flex;align-items:center;height:24px;padding:0 10px;border-radius:999px;background:rgba(var(--fg),.06);color:var(--t2);font-size:11.5px;white-space:nowrap}
textarea{margin:14px 0 0;flex:1;min-height:120px;resize:none;border-radius:14px;padding:14px 16px;background:var(--sunk);border:1px solid rgba(var(--fg),.05);box-shadow:inset 0 2px 10px rgba(0,0,0,calc(.5*var(--shk)));color:var(--t1);font:400 14px/1.5 Manrope,system-ui,sans-serif;outline:0}
textarea:focus{border-color:rgba(var(--fg),.25)}
.arow{display:flex;gap:8px;margin-top:12px;align-items:center}
.arow .pill{height:40px}
.hint{font-size:12px;color:var(--t3)}
</style>
