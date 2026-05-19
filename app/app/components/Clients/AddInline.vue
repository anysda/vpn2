<script setup lang="ts">
import { useClients } from '~/composables/useClients'
import { useTelegramStatus } from '~/composables/useTelegramStatus'

const { create } = useClients()
const { canSend: canSendTg, reason: tgReason } = useTelegramStatus()
const toast = useToast()

const open = ref(false)
const name = ref('')
const expiresAt = ref<string>('')
const sendToTg = ref(false)
const loading = ref(false)

async function submit() {
  if (!name.value.trim()) return
  loading.value = true
  try {
    await create({
      name: name.value.trim(),
      expiresAt: expiresAt.value ? new Date(expiresAt.value).toISOString() : null,
      sendToTg: sendToTg.value,
    })
    toast.add({
      title: sendToTg.value
        ? `Клиент «${name.value}» создан, конфиг отправлен в Telegram`
        : `Клиент «${name.value}» создан`,
      color: 'success',
    })
    name.value = ''
    expiresAt.value = ''
    sendToTg.value = false
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
  sendToTg.value = false
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
      <div class="flex items-center justify-between pt-1">
        <UTooltip :text="tgReason" :disabled="canSendTg">
          <label
            :class="[
              'text-sm flex items-center gap-2',
              canSendTg ? 'cursor-pointer' : 'cursor-not-allowed opacity-50',
            ]"
          >
            <UIcon name="i-simple-icons-telegram" class="text-blue-400" />
            Отправить конфиг в Telegram
          </label>
        </UTooltip>
        <USwitch
          v-model="sendToTg"
          :disabled="!canSendTg"
        />
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
