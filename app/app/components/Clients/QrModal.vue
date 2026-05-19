<script setup lang="ts">
import type { Client } from '~/composables/useClients'
import { useClients } from '~/composables/useClients'

const props = defineProps<{ client: Client }>()
const open = defineModel<boolean>('open', { default: false })

const { getSsUrl, generateOneTimeLink } = useClients()
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
  if (now) {
    loadUrl()
  }
  else {
    if (otlTimer) clearInterval(otlTimer)
    otlTimer = null
    otl.value = null
    otlRemaining.value = 0
  }
})

async function copyUrl() {
  if (!ssUrl.value) return
  await navigator.clipboard.writeText(ssUrl.value)
  toast.add({ title: 'ss:// URL скопирован', color: 'success' })
}

async function download() {
  if (!ssUrl.value) return
  const blob = new Blob([ssUrl.value], { type: 'text/plain' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${props.client.name}.outline.txt`
  a.click()
  URL.revokeObjectURL(a.href)
}

const otl = ref<{ url: string, expiresAt: string, ttlSeconds: number } | null>(null)
const otlLoading = ref(false)
const otlRemaining = ref(0)
let otlTimer: ReturnType<typeof setInterval> | null = null

const otlFullUrl = computed(() => {
  if (!otl.value || typeof window === 'undefined') return ''
  return window.location.origin + otl.value.url
})

const otlRemainingLabel = computed(() => {
  const m = Math.floor(otlRemaining.value / 60)
  const s = otlRemaining.value % 60
  return `${m}:${s.toString().padStart(2, '0')}`
})

async function genOtl() {
  otlLoading.value = true
  try {
    const r = await generateOneTimeLink(props.client.id)
    otl.value = r
    otlRemaining.value = r.ttlSeconds
    if (otlTimer) clearInterval(otlTimer)
    otlTimer = setInterval(() => {
      otlRemaining.value--
      if (otlRemaining.value <= 0) {
        clearInterval(otlTimer!)
        otlTimer = null
      }
    }, 1000)
  }
  catch (e) {
    toast.add({ title: 'Не удалось создать ссылку', color: 'error', description: (e as Error).message })
  }
  finally {
    otlLoading.value = false
  }
}

async function copyOtl() {
  if (!otlFullUrl.value) return
  await navigator.clipboard.writeText(otlFullUrl.value)
  toast.add({ title: 'Ссылка скопирована', color: 'success' })
}

onUnmounted(() => {
  if (otlTimer) clearInterval(otlTimer)
})
</script>

<template>
  <UModal v-model:open="open" :title="client.name" :ui="{ content: 'max-w-md' }">
    <template #body>
      <div class="space-y-4">
        <div class="flex justify-center">
          <img
            :src="`/api/clients/${client.id}/qrcode.svg`"
            alt="QR"
            class="w-64 h-64 rounded-md bg-white p-2"
          >
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
            icon="i-lucide-download"
            variant="soft"
            color="neutral"
            :disabled="!ssUrl"
            @click="download"
          >
            Скачать
          </UButton>
        </div>

        <USeparator label="или одноразовая ссылка (5 мин)" />

        <div v-if="!otl">
          <UButton
            block
            variant="outline"
            icon="i-lucide-link"
            :loading="otlLoading"
            @click="genOtl"
          >
            Сгенерировать одноразовую ссылку
          </UButton>
        </div>

        <div v-else class="space-y-2">
          <div class="text-xs text-(--ui-text-muted) flex items-center gap-1">
            <UIcon name="i-lucide-timer" />
            Истекает через {{ otlRemainingLabel }}
          </div>
          <div class="flex gap-2">
            <UInput
              :model-value="otlFullUrl"
              readonly
              class="flex-1 font-mono text-xs"
            />
            <UButton icon="i-lucide-copy" @click="copyOtl" />
          </div>
        </div>
      </div>
    </template>
  </UModal>
</template>
