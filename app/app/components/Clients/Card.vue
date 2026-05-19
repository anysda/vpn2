<script setup lang="ts">
import type { Client } from '~/composables/useClients'
import { expiryLabel, useClients } from '~/composables/useClients'

const props = defineProps<{
  client: Client
  traffic?: { rxBytes: number, txBytes: number } | null
}>()
const emit = defineEmits<{ deleted: [id: number] }>()

const { update, remove, getSsUrl } = useClients()
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

async function downloadConfig() {
  try {
    const url = await getSsUrl(props.client.id)
    const blob = new Blob([url], { type: 'text/plain' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${props.client.name}.outline.txt`
    a.click()
    URL.revokeObjectURL(a.href)
  }
  catch (e) {
    toast.add({ title: 'Ошибка скачивания', color: 'error', description: (e as Error).message })
  }
}

async function copySsUrl() {
  try {
    const url = await getSsUrl(props.client.id)
    await navigator.clipboard.writeText(url)
    toast.add({ title: 'ss:// URL скопирован', color: 'success' })
  }
  catch (e) {
    toast.add({ title: 'Ошибка копирования', color: 'error', description: (e as Error).message })
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
    class="rounded-md border border-zinc-800 bg-zinc-900/60 p-3"
  >
    <div class="flex items-start gap-3">
      <UAvatar :alt="client.name" size="md" />
      <div class="flex-1 min-w-0">
        <div class="font-medium truncate text-zinc-100">
          {{ client.name }}
        </div>
        <div class="text-xs text-zinc-500 mt-0.5">
          {{ expiryLabel(client.expiresAt) }}
        </div>
        <div v-if="traffic" class="text-xs text-zinc-400 mt-1 flex gap-3">
          <span>↓ {{ fmtBytes(traffic.rxBytes) }}</span>
          <span>↑ {{ fmtBytes(traffic.txBytes) }}</span>
        </div>
      </div>
      <USwitch
        :model-value="enabled"
        @update:model-value="onToggle"
      />
    </div>

    <div class="flex gap-1 mt-3 justify-end">
      <UTooltip text="Копировать ss:// URL">
        <UButton icon="i-lucide-link" size="xs" color="neutral" variant="ghost" @click="copySsUrl" />
      </UTooltip>
      <UTooltip text="Редактировать">
        <UButton icon="i-lucide-pencil" size="xs" color="neutral" variant="ghost" @click="showEdit = true" />
      </UTooltip>
      <UTooltip text="QR / одноразовая ссылка">
        <UButton icon="i-lucide-qr-code" size="xs" color="neutral" variant="ghost" @click="showQr = true" />
      </UTooltip>
      <UTooltip text="Скачать конфиг">
        <UButton icon="i-lucide-download" size="xs" color="neutral" variant="ghost" @click="downloadConfig" />
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
