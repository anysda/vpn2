<script setup lang="ts">
import { parseRuDate, useClients } from '~/composables/useClients'
import { useTelegramStatus } from '~/composables/useTelegramStatus'

const { create, sendPasswordToTg } = useClients()
const { canSend: canSendTg, reason: tgReason } = useTelegramStatus()
const toast = useToast()

const DEFAULT_DEVICE_LIMIT = 3

const open = ref(false)
const name = ref('')
const filterTraffic = ref(true)
const sendPwdToTg = ref(false)
const expiresAt = ref('')
const deviceLimit = ref<number>(DEFAULT_DEVICE_LIMIT)
const unlimited = ref(false)
const loading = ref(false)

function toggleUnlimited() {
  unlimited.value = !unlimited.value
  if (!unlimited.value) deviceLimit.value = DEFAULT_DEVICE_LIMIT
}

// При безлимите поле пустое — тогда виден плейсхолдер «∞».
const limitInput = computed<number | undefined>({
  get: () => (unlimited.value ? undefined : deviceLimit.value),
  set: (v) => {
    if (typeof v === 'number' && !Number.isNaN(v)) deviceLimit.value = v
  },
})

async function submit() {
  if (!name.value.trim()) return
  let expiresIso: string | null = null
  if (expiresAt.value.trim()) {
    expiresIso = parseRuDate(expiresAt.value)
    if (!expiresIso) {
      toast.add({ title: 'Срок действия — в формате ДД.ММ.ГГГГ', color: 'error' })
      return
    }
  }
  loading.value = true
  try {
    const client = await create({
      name: name.value.trim(),
      filterTraffic: filterTraffic.value,
      expiresAt: expiresIso,
      deviceLimit: unlimited.value
        ? null
        : Math.max(1, Math.floor(deviceLimit.value || DEFAULT_DEVICE_LIMIT)),
    })
    if (sendPwdToTg.value && canSendTg.value) {
      try {
        await sendPasswordToTg(client.id)
      }
      catch {
        toast.add({ title: 'Клиент создан, но пароль не отправлен в Telegram', color: 'warning' })
      }
    }
    toast.add({ title: `Клиент «${name.value.trim()}» создан`, color: 'success' })
    reset()
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
  filterTraffic.value = true
  sendPwdToTg.value = false
  expiresAt.value = ''
  deviceLimit.value = DEFAULT_DEVICE_LIMIT
  unlimited.value = false
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
          placeholder="Иван Иванов"
          autofocus
          class="w-full"
        />
      </UFormField>

      <div class="grid grid-cols-2 gap-2">
        <UFormField label="Срок действия">
          <DateInput v-model="expiresAt" class="w-full" />
        </UFormField>
        <UFormField label="Лимит девайсов">
          <div class="flex gap-1">
            <UInput
              v-model.number="limitInput"
              type="text"
              inputmode="numeric"
              :disabled="unlimited"
              :placeholder="unlimited ? '∞' : ''"
              class="flex-1 min-w-0"
            />
            <UTooltip text="Безлимитно">
              <UButton
                icon="i-lucide-infinity"
                :color="unlimited ? 'primary' : 'neutral'"
                :variant="unlimited ? 'solid' : 'soft'"
                @click="toggleUnlimited"
              />
            </UTooltip>
          </div>
        </UFormField>
      </div>

      <div class="flex items-center justify-between pt-1">
        <label class="text-sm flex items-center gap-2 cursor-pointer">
          <UIcon name="i-simple-icons-adguard" class="size-4 text-[#67b279]" />
          Фильтровать трафик (AdGuard)
        </label>
        <USwitch v-model="filterTraffic" />
      </div>

      <USeparator />

      <div class="flex items-center justify-between">
        <UTooltip :text="tgReason" :disabled="canSendTg">
          <label
            class="text-sm flex items-center gap-2"
            :class="canSendTg ? 'cursor-pointer' : 'opacity-60'"
          >
            <UIcon name="i-simple-icons-telegram" class="size-4 text-[#26A5E4]" />
            Отправить доступ к боту
          </label>
        </UTooltip>
        <USwitch v-model="sendPwdToTg" :disabled="!canSendTg" />
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
