<script setup lang="ts">
import type { Client } from '~/composables/useClients'
import { useClientsOvpn } from '~/composables/useClientsOvpn'
import { useTelegramStatus } from '~/composables/useTelegramStatus'

const props = defineProps<{ client: Client }>()
const open = defineModel<boolean>('open', { default: false })

const { getOvpnConfig, sendOvpnToTg } = useClientsOvpn()
const { canSend: canSendTg, reason: tgReason } = useTelegramStatus()
const toast = useToast()

const conf = ref<string>('')
const loadingConf = ref(false)

async function loadConf() {
  if (conf.value) return
  loadingConf.value = true
  try {
    conf.value = await getOvpnConfig(props.client.id)
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Не удалось получить .ovpn', color: 'error' })
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
  await navigator.clipboard.writeText(conf.value)
  toast.add({ title: '.ovpn скопирован', color: 'success' })
}

function download() {
  if (!conf.value) return
  const blob = new Blob([conf.value], { type: 'application/x-openvpn-profile' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${props.client.name}.ovpn`
  a.click()
  URL.revokeObjectURL(a.href)
}

const sendingTg = ref(false)
async function sendToTelegram() {
  sendingTg.value = true
  try {
    await sendOvpnToTg(props.client.id)
    toast.add({ title: 'OpenVPN-конфиг отправлен в Telegram', color: 'success' })
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
          <OpenVpnLogo class="size-6" />
          <span class="font-semibold text-(--ui-text-highlighted)">OpenVPN</span>
        </div>

        <p class="text-xs text-(--ui-text-muted) text-center">
          .ovpn-файл с встроенными сертификатами — импортируй в OpenVPN Connect
          или <span class="font-mono">openvpn3</span>.
        </p>

        <div
          v-if="loadingConf"
          class="text-(--ui-text-muted) text-sm text-center py-4"
        >
          генерирую сертификат…
        </div>

        <div v-else class="grid grid-cols-3 gap-2">
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
