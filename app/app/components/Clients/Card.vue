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
const showWg = ref(false)
const showOvpn = ref(false)
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

// Суммарный трафик клиента (rx+tx) за всё время.
const trafficTotal = computed(() =>
  (props.traffic?.rxBytes ?? 0) + (props.traffic?.txBytes ?? 0),
)
</script>

<template>
  <div
    class="rounded-md border border-(--ui-border) bg-(--ui-page) px-3 py-2"
  >
    <div class="flex items-center gap-3">
      <div class="flex-1 min-w-0">
        <div class="font-medium text-(--ui-text-highlighted) leading-tight flex items-center gap-1.5">
          <UTooltip
            v-if="client.filterTraffic"
            text="Фильтрация рекламы и трекеров включена"
          >
            <UIcon
              name="i-simple-icons-adguard"
              class="size-4 text-[#67b279] shrink-0"
            />
          </UTooltip>
          <span class="truncate">{{ client.name }}</span>
        </div>
        <div class="text-xs text-(--ui-text-muted) flex gap-3 items-center mt-0.5">
          <span>{{ expiryLabel(client.expiresAt) }}</span>
          <UTooltip
            v-if="trafficTotal > 0"
            :text="`Трафик за всё время (все протоколы) · ↓ ${fmtBytes(traffic?.rxBytes)} ↑ ${fmtBytes(traffic?.txBytes)}`"
          >
            <span class="flex items-center gap-0.5">
              <UIcon name="i-lucide-arrow-down" class="size-3 shrink-0" />
              {{ fmtBytes(trafficTotal) }}
            </span>
          </UTooltip>
        </div>
      </div>
      <USwitch
        :model-value="enabled"
        @update:model-value="onToggle"
      />
    </div>

    <div class="flex gap-1 mt-1.5 justify-end">
      <UTooltip text="WireGuard — QR, .conf, копировать, отправить в Telegram">
        <UButton size="xs" color="neutral" variant="ghost" class="!px-1" @click="showWg = true">
          <WireguardLogo class="size-4" />
        </UButton>
      </UTooltip>
      <UTooltip text="OpenVPN — .ovpn-файл, копировать, отправить в Telegram">
        <UButton size="xs" color="neutral" variant="ghost" class="!px-1" @click="showOvpn = true">
          <OpenVpnLogo class="size-4" />
        </UButton>
      </UTooltip>
      <UTooltip text="Редактировать">
        <UButton icon="i-lucide-pencil" size="xs" color="neutral" variant="ghost" @click="showEdit = true" />
      </UTooltip>
      <UTooltip text="Удалить">
        <UButton icon="i-lucide-trash-2" size="xs" color="error" variant="ghost" @click="confirmDelete = true" />
      </UTooltip>
    </div>

    <ClientsWireguardModal v-model:open="showWg" :client="client" />
    <ClientsOpenVpnModal v-model:open="showOvpn" :client="client" />
    <ClientsEditDialog v-model:open="showEdit" :client="client" />

    <UModal v-model:open="confirmDelete" title="Удалить клиента?">
      <template #body>
        <p class="text-sm">
          Удалить <span class="font-semibold">«{{ client.name }}»</span>?
          Действие необратимо, конфиги клиента сразу перестанут работать.
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
