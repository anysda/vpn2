<script setup lang="ts">
import type { Client } from '~/composables/useClients'
import { useClientsWg } from '~/composables/useClientsWg'
import { useTelegramStatus } from '~/composables/useTelegramStatus'
import { copyText } from '~/utils/clipboard'

const props = defineProps<{ client: Client }>()
const open = defineModel<boolean>('open', { default: false })

const { getWgConfig, sendWgToTg } = useClientsWg()
const { canSend: canSendTg, reason: tgReason } = useTelegramStatus()
const toast = useToast()

const conf = ref<string>('')
const loadingConf = ref(false)

async function loadConf() {
  if (conf.value) return
  loadingConf.value = true
  try {
    conf.value = await getWgConfig(props.client.id)
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Не удалось получить .conf', color: 'error' })
  }
  finally {
    loadingConf.value = false
  }
}

watch(open, (now) => {
  if (now) loadConf()
})

async function copyConf() {
  if (!conf.value) return
  if (await copyText(conf.value))
    toast.add({ title: '.conf скопирован', color: 'success' })
  else
    toast.add({ title: 'Не удалось скопировать', color: 'error' })
}

function download() {
  if (!conf.value) return
  const blob = new Blob([conf.value], { type: 'text/plain' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${props.client.name}.conf`
  a.click()
  URL.revokeObjectURL(a.href)
}

const sendingTg = ref(false)
async function sendToTelegram() {
  sendingTg.value = true
  try {
    await sendWgToTg(props.client.id)
    toast.add({ title: 'WireGuard-конфиг отправлен в Telegram', color: 'success' })
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
          <WireguardLogo class="size-6" />
          <span class="font-semibold text-(--ui-text-highlighted)">WireGuard</span>
        </div>

        <div class="flex justify-center">
          <img
            :src="`/api/clients/${client.id}/wg-qrcode.svg`"
            alt="WireGuard QR"
            class="w-64 h-64 rounded-md bg-white p-2"
          >
        </div>

        <div class="grid grid-cols-3 gap-2">
          <UButton
            block
            icon="i-lucide-copy"
            :disabled="!conf"
            @click="copyConf"
          >
            Копировать
          </UButton>
          <UButton
            block
            icon="i-lucide-download"
            variant="soft"
            color="neutral"
            :disabled="!conf"
            @click="download"
          >
            Скачать
          </UButton>
          <UTooltip :text="tgReason" :disabled="canSendTg" class="block">
            <UButton
              block
              icon="i-lucide-send"
              variant="soft"
              color="neutral"
              :disabled="!conf || !canSendTg"
              :loading="sendingTg"
              @click="sendToTelegram"
            >
              В TG
            </UButton>
          </UTooltip>
        </div>
      </div>
    </template>
  </UModal>
</template>
