<script setup lang="ts">
import { useClients } from '~/composables/useClients'
import { useTelegramStatus } from '~/composables/useTelegramStatus'

const { create } = useClients()
const { canSend: canSendTg, reason: tgReason } = useTelegramStatus()
const toast = useToast()

const open = ref(false)
const name = ref('')
const expiresAt = ref<string>('')
const sendWgToTg = ref(false)
const sendSsToTg = ref(false)
const sendOvpnToTg = ref(false)
const loading = ref(false)

async function submit() {
  if (!name.value.trim()) return
  loading.value = true
  try {
    await create({
      name: name.value.trim(),
      expiresAt: expiresAt.value ? new Date(expiresAt.value).toISOString() : null,
      sendSsToTg: sendSsToTg.value,
      sendWgToTg: sendWgToTg.value,
      sendOvpnToTg: sendOvpnToTg.value,
    })
    const tgParts = [
      sendWgToTg.value && 'WireGuard',
      sendSsToTg.value && 'Outline',
      sendOvpnToTg.value && 'OpenVPN',
    ].filter(Boolean)
    toast.add({
      title: tgParts.length
        ? `Клиент «${name.value}» создан, ${tgParts.join(' + ')} отправлены в Telegram`
        : `Клиент «${name.value}» создан`,
      color: 'success',
    })
    name.value = ''
    expiresAt.value = ''
    sendWgToTg.value = false
    sendSsToTg.value = false
    sendOvpnToTg.value = false
    open.value = false
  }
  catch (e) {
    const err = e as { statusMessage?: string, message?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка', color: 'error' })
  }
  finally {
    loading.value = false
  }
}

function reset() {
  name.value = ''
  expiresAt.value = ''
  sendWgToTg.value = false
  sendSsToTg.value = false
  sendOvpnToTg.value = false
  open.value = false
}
</script>

<template>
  <div>
    <UButton
      v-if="!open"
      block
      icon="i-lucide-plus"
      color="primary"
      variant="soft"
      @click="open = true"
    >
      Новый клиент
    </UButton>

    <form
      v-else
      class="space-y-2 p-2 border border-(--ui-border) rounded-md"
      @submit.prevent="submit"
    >
      <UFormField label="Имя">
        <UInput
          v-model="name"
          placeholder="напр. anysda-phone"
          autofocus
          class="w-full"
        />
      </UFormField>
      <UFormField label="Истекает (опционально)">
        <UInput
          v-model="expiresAt"
          type="date"
          class="w-full"
        />
      </UFormField>
      <div class="space-y-2 pt-1">
        <UTooltip :text="tgReason" :disabled="canSendTg">
          <div :class="['flex items-center justify-between', !canSendTg && 'opacity-50']">
            <label class="text-sm flex items-center gap-2 cursor-pointer">
              <WireguardLogo class="size-4" />
              Отправить WireGuard в Telegram
            </label>
            <USwitch v-model="sendWgToTg" :disabled="!canSendTg" />
          </div>
        </UTooltip>
        <UTooltip :text="tgReason" :disabled="canSendTg">
          <div :class="['flex items-center justify-between', !canSendTg && 'opacity-50']">
            <label class="text-sm flex items-center gap-2 cursor-pointer">
              <OutlineLogo class="size-4" />
              Отправить Outline в Telegram
            </label>
            <USwitch v-model="sendSsToTg" :disabled="!canSendTg" />
          </div>
        </UTooltip>
        <UTooltip :text="tgReason" :disabled="canSendTg">
          <div :class="['flex items-center justify-between', !canSendTg && 'opacity-50']">
            <label class="text-sm flex items-center gap-2 cursor-pointer">
              <OpenVpnLogo class="size-4" />
              Отправить OpenVPN в Telegram
            </label>
            <USwitch v-model="sendOvpnToTg" :disabled="!canSendTg" />
          </div>
        </UTooltip>
      </div>
      <div class="flex gap-2">
        <UButton
          color="neutral"
          variant="soft"
          :disabled="loading"
          @click="reset"
        >
          Отмена
        </UButton>
        <UButton
          type="submit"
          color="primary"
          :loading="loading"
          :disabled="!name.trim()"
        >
          Создать
        </UButton>
      </div>
    </form>
  </div>
</template>
