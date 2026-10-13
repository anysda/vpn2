<script setup lang="ts">
import { botDeliveryText, botQueueText, type BotDelivery } from '#shared/bot-delivery'
interface TgStatus {
  configured: boolean
  running: boolean
  bot_token_masked: string
  chat_id: string | number | ''
  admin_username: string
}

const { data, refresh } = useFetch<TgStatus>('/api/admin/telegram', {
  default: () => ({ configured: false, running: false, bot_token_masked: '', chat_id: '', admin_username: '' }),
  server: false,
})

const toast = useToast()
const { queued, show: showDelivery } = useBotDeliveryToast()

const newToken = ref('')
const newChatId = ref('')
const newAdminUsername = ref('')
const saving = ref(false)

watch(data, (d) => {
  if (d) {
    newChatId.value = String(d.chat_id ?? '')
    newAdminUsername.value = String(d.admin_username ?? '')
    newToken.value = ''
  }
}, { immediate: true })

const statusBadge = computed(() => {
  if (!data.value?.configured) return { label: 'НЕНАСТРОЕН', color: 'neutral' as const }
  if (data.value.running) return { label: 'ОНЛАЙН', color: 'success' as const }
  return { label: 'ОФФЛАЙН', color: 'error' as const }
})

const dirty = computed(() =>
  !!newToken.value.trim()
  || newChatId.value.trim() !== String(data.value?.chat_id ?? '')
  || newAdminUsername.value.trim() !== String(data.value?.admin_username ?? ''),
)

async function save() {
  saving.value = true
  try {
    const body: Record<string, string> = {}
    if (newToken.value.trim()) body.bot_token = newToken.value.trim()
    if (newChatId.value.trim() !== String(data.value?.chat_id ?? '')) {
      body.chat_id = newChatId.value.trim()
    }
    if (newAdminUsername.value.trim() !== String(data.value?.admin_username ?? '')) {
      body.admin_username = newAdminUsername.value.trim()
    }
    if (Object.keys(body).length === 0) {
      saving.value = false
      return
    }
    await $fetch('/api/admin/telegram', { method: 'PUT', body })
    toast.add({ title: 'Сохранено', color: 'success' })
    newToken.value = ''
    await refresh()
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: 'Ошибка', color: 'error', description: err.statusMessage })
  }
  finally {
    saving.value = false
  }
}

let timer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  if (!timer) timer = setInterval(() => { void refresh() }, 10_000)
})
onUnmounted(() => { if (timer) { clearInterval(timer); timer = null } })
useVisibleRefresh(refresh)

const message = ref('')
const sending = ref(false)
async function sendMessage() {
  const text = message.value.trim()
  if (!text) return
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
  <UCard>
    <template #header>
      <div class="font-semibold">
        Боты
      </div>
    </template>

    <div class="space-y-3 text-sm">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <UIcon name="i-simple-icons-telegram" class="text-blue-400" />
          <span class="font-medium">Telegram</span>
        </div>
        <UBadge :color="statusBadge.color" variant="subtle" size="sm">
          {{ statusBadge.label }}
        </UBadge>
      </div>

      <p v-if="queued" role="status" class="text-(--ui-warning)">{{ botQueueText(queued) }}</p>

      <UFormField label="Token" :ui="{ label: 'text-xs text-(--ui-text-muted)' }">
        <UInput
          v-model="newToken"
          autocomplete="off"
          data-1p-ignore
          data-lpignore="true"
          data-bwignore
          type="text"
          :placeholder="data?.bot_token_masked || ''"
          class="w-full font-mono text-xs"
        />
      </UFormField>

      <UFormField label="Chat ID" :ui="{ label: 'text-xs text-(--ui-text-muted)' }">
        <UInput
          v-model="newChatId"
          autocomplete="off"
          data-1p-ignore
          data-lpignore="true"
          data-bwignore
          placeholder=""
          inputmode="numeric"
          class="w-full font-mono text-xs"
        />
      </UFormField>

      <UFormField
        label="Никнейм админа бота"
        :ui="{ label: 'text-xs text-(--ui-text-muted)' }"
      >
        <UInput
          v-model="newAdminUsername"
          autocomplete="off"
          data-1p-ignore
          data-lpignore="true"
          data-bwignore
          placeholder="@ник"
          class="w-full font-mono text-xs"
        />
      </UFormField>

      <div class="flex justify-end">
        <UButton
          size="xs"
          :loading="saving"
          :disabled="!dirty"
          @click="save"
        >
          Сохранить
        </UButton>
      </div>

      <UFormField label="Сообщение юзерам" :ui="{ label: 'text-xs text-(--ui-text-muted)' }">
        <UTextarea
          v-model="message"
          autocomplete="off"
          data-1p-ignore
          data-lpignore="true"
          data-bwignore
          autoresize
          :rows="2"
          :maxlength="4096"
          class="w-full text-xs"
        />
      </UFormField>

      <div v-if="message.trim()" class="flex justify-end">
        <UButton
          size="xs"
          icon="i-lucide-send"
          :loading="sending"
          @click="sendMessage"
        >
          Отправить
        </UButton>
      </div>
    </div>
  </UCard>
</template>
