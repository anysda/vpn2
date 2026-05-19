<script setup lang="ts">
interface TgStatus {
  configured: boolean
  running: boolean
  bot_token_masked: string
  chat_id: string | number | ''
}

const { data, refresh } = useFetch<TgStatus>('/api/admin/telegram', {
  default: () => ({ configured: false, running: false, bot_token_masked: '', chat_id: '' }),
  server: false,
})

const toast = useToast()

const newToken = ref('')
const newChatId = ref('')
const editing = ref(false)
const showToken = ref(false)
const saving = ref(false)

watch(data, (d) => {
  if (d) {
    newChatId.value = String(d.chat_id ?? '')
    newToken.value = ''
  }
}, { immediate: true })

const statusBadge = computed(() => {
  if (!data.value?.configured) return { label: 'НЕНАСТРОЕН', color: 'neutral' as const }
  if (data.value.running) return { label: 'ОНЛАЙН', color: 'success' as const }
  return { label: 'ОФФЛАЙН', color: 'error' as const }
})

async function save() {
  saving.value = true
  try {
    const body: Record<string, string> = {}
    if (newToken.value.trim()) body.bot_token = newToken.value.trim()
    if (newChatId.value.trim() !== String(data.value?.chat_id ?? '')) {
      body.chat_id = newChatId.value.trim()
    }
    if (Object.keys(body).length === 0) {
      editing.value = false
      saving.value = false
      return
    }
    await $fetch('/api/admin/telegram', { method: 'PUT', body })
    toast.add({ title: 'Сохранено', color: 'success', description: 'Бот рестартит автоматически через ~10с' })
    newToken.value = ''
    editing.value = false
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
          <UIcon name="i-simple-icons-telegram" class="text-blue-500" />
          <span class="font-medium">Telegram</span>
        </div>
        <UBadge :color="statusBadge.color" variant="subtle" size="sm">
          {{ statusBadge.label }}
        </UBadge>
      </div>

      <p class="text-xs text-(--ui-text-muted)">
        Изменения применяются автоматически, бот рестартит через ~10с.
      </p>

      <UFormField label="Token" :ui="{ label: 'text-xs text-(--ui-text-muted)' }">
        <UInput
          v-model="newToken"
          :type="showToken ? 'text' : 'password'"
          :placeholder="data?.bot_token_masked || 'нажми чтобы заполнить'"
          class="w-full font-mono text-xs"
        >
          <template #trailing>
            <UButton
              :icon="showToken ? 'i-lucide-eye-off' : 'i-lucide-eye'"
              size="xs"
              color="neutral"
              variant="ghost"
              @click="showToken = !showToken"
            />
          </template>
        </UInput>
      </UFormField>

      <UFormField label="Chat ID" :ui="{ label: 'text-xs text-(--ui-text-muted)' }">
        <UInput
          v-model="newChatId"
          placeholder="напр. 1234567890 или -1001234..."
          inputmode="numeric"
          class="w-full font-mono text-xs"
        />
      </UFormField>

      <div class="flex justify-end">
        <UButton
          size="xs"
          :loading="saving"
          :disabled="!newToken.trim() && newChatId.trim() === String(data?.chat_id ?? '')"
          @click="save"
        >
          Сохранить
        </UButton>
      </div>
    </div>
  </UCard>
</template>
