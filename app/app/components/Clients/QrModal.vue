<script setup lang="ts">
import type { Client } from '~/composables/useClients'
import { useClients } from '~/composables/useClients'

const props = defineProps<{ client: Client }>()
const open = defineModel<boolean>('open', { default: false })

const { getSsUrl, sendToTg } = useClients()
const toast = useToast()

const ssUrl = ref<string>('')
const loadingUrl = ref(false)

async function loadUrl() {
  if (ssUrl.value) return
  loadingUrl.value = true
  try {
    ssUrl.value = await getSsUrl(props.client.id)
  }
  catch (e) {
    toast.add({ title: 'Не удалось получить ss:// URL', color: 'error', description: (e as Error).message })
  }
  finally {
    loadingUrl.value = false
  }
}

watch(open, (now) => {
  if (now) loadUrl()
})

async function copyUrl() {
  if (!ssUrl.value) return
  await navigator.clipboard.writeText(ssUrl.value)
  toast.add({ title: 'ss:// URL скопирован', color: 'success' })
}

const sendingTg = ref(false)
async function sendToTelegram() {
  sendingTg.value = true
  try {
    await sendToTg(props.client.id)
    toast.add({ title: 'Конфиг отправлен в Telegram', color: 'success' })
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка отправки', color: 'error' })
  }
  finally {
    sendingTg.value = false
  }
}
</script>

<template>
  <UModal v-model:open="open" :title="client.name" :ui="{ content: 'max-w-md' }">
    <template #body>
      <div class="space-y-4">
        <div class="flex items-center justify-center gap-2">
          <OutlineLogo class="size-6" />
          <span class="font-semibold text-(--ui-text-highlighted)">Outline</span>
        </div>

        <UInput
          :model-value="ssUrl"
          readonly
          :loading="loadingUrl"
          placeholder="Загрузка..."
          class="w-full font-mono text-xs"
        />

        <div class="grid grid-cols-2 gap-2">
          <UButton
            block
            icon="i-lucide-copy"
            :disabled="!ssUrl"
            @click="copyUrl"
          >
            Копировать
          </UButton>
          <UButton
            block
            icon="i-simple-icons-telegram"
            variant="soft"
            color="neutral"
            :disabled="!ssUrl"
            :loading="sendingTg"
            @click="sendToTelegram"
          >
            Telegram
          </UButton>
        </div>
      </div>
    </template>
  </UModal>
</template>
