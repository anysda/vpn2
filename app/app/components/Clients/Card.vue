<script setup lang="ts">
import type { Client } from '~/composables/useClients'
import { expiryLabel, useClients } from '~/composables/useClients'

const props = defineProps<{
  client: Client
  traffic?: { rxBytes: number, txBytes: number } | null
}>()
const emit = defineEmits<{ deleted: [id: number] }>()

const { update, remove } = useClients()
const toast = useToast()

const enabled = ref(props.client.enabled)
watch(() => props.client.enabled, v => enabled.value = v)

async function onToggle(next: boolean) {
  enabled.value = next
  try {
    await update(props.client.id, { enabled: next })
  }
  catch (e) {
    enabled.value = !next
    toast.add({ title: 'Не удалось переключить', color: 'error', description: (e as Error).message })
  }
}

const confirmDelete = ref(false)
const showQr = ref(false)
const showEdit = ref(false)

async function doDelete() {
  try {
    await remove(props.client.id)
    emit('deleted', props.client.id)
    toast.add({ title: `«${props.client.name}» удалён`, color: 'success' })
  }
  catch (e) {
    toast.add({ title: 'Ошибка удаления', color: 'error', description: (e as Error).message })
  }
  finally {
    confirmDelete.value = false
  }
}

function fmtBytes(n: number | undefined | null): string {
  if (!n) return '0'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(2)} GB`
}
</script>

<template>
  <div
    class="rounded-md border border-(--ui-border) bg-(--ui-page) px-3 py-2"
  >
    <div class="flex items-center gap-3">
      <div class="flex-1 min-w-0">
        <div class="font-medium truncate text-(--ui-text-highlighted) leading-tight">
          {{ client.name }}
        </div>
        <div class="text-xs text-(--ui-text-muted) flex gap-2 items-center mt-0.5">
          <span>{{ expiryLabel(client.expiresAt) }}</span>
          <template v-if="traffic">
            <span aria-hidden="true">·</span>
            <span>↓ {{ fmtBytes(traffic.rxBytes) }}</span>
            <span>↑ {{ fmtBytes(traffic.txBytes) }}</span>
          </template>
        </div>
      </div>
      <USwitch
        :model-value="enabled"
        @update:model-value="onToggle"
      />
    </div>

    <div class="flex gap-1 mt-1.5 justify-end">
      <UTooltip text="Outline — копировать URL или отправить в Telegram">
        <UButton size="xs" color="neutral" variant="ghost" class="!px-1" @click="showQr = true">
          <OutlineLogo class="size-4" />
        </UButton>
      </UTooltip>
      <UTooltip text="Редактировать">
        <UButton icon="i-lucide-pencil" size="xs" color="neutral" variant="ghost" @click="showEdit = true" />
      </UTooltip>
      <UTooltip text="Удалить">
        <UButton icon="i-lucide-trash-2" size="xs" color="error" variant="ghost" @click="confirmDelete = true" />
      </UTooltip>
    </div>

    <ClientsQrModal v-model:open="showQr" :client="client" />
    <ClientsEditDialog v-model:open="showEdit" :client="client" />

    <UModal v-model:open="confirmDelete" title="Удалить клиента?">
      <template #body>
        <p class="text-sm">
          Удалить <span class="font-semibold">«{{ client.name }}»</span>?
          Действие необратимо, ss-URL клиента сразу перестанет работать.
        </p>
      </template>
      <template #footer>
        <UButton color="neutral" variant="soft" @click="confirmDelete = false">
          Отмена
        </UButton>
        <UButton color="error" @click="doDelete">
          Удалить
        </UButton>
      </template>
    </UModal>
  </div>
</template>
