<script setup lang="ts">
import { useClients } from '~/composables/useClients'
import type { ClientTraffic } from '~/composables/useClients'

useHead({ title: 'anysda-vpn2' })

const { clients, refresh: refreshClients } = useClients()
const search = ref('')

// Сверху — кто «в сети» (трафик за 5 мин), дальше по объёму за сутки, затем по имени.
const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  const list = q ? clients.value.filter(c => c.name.toLowerCase().includes(q)) : [...clients.value]
  const t = trafficMap.value ?? {}
  const day = (id: number) => (t[id]?.dayRx ?? 0) + (t[id]?.dayTx ?? 0)
  return list.sort((a, b) =>
    Number(t[b.id]?.online ?? false) - Number(t[a.id]?.online ?? false)
    || day(b.id) - day(a.id)
    || a.name.localeCompare(b.name, 'ru'),
  )
})

// Модалка клиента, открывается кликом по карточке.
const modalOpen = ref(false)
const selectedClientId = ref<number | null>(null)
function openClient(id: number) {
  selectedClientId.value = id
  modalOpen.value = true
}

const { data: trafficMap, refresh: refreshTraffic } = useFetch<Record<number, ClientTraffic>>('/api/clients/traffic', {
  default: () => ({}),
  server: false,
})

let trafficTimer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  if (!trafficTimer) trafficTimer = setInterval(() => { void refreshTraffic() }, 3000)
})
onUnmounted(() => { if (trafficTimer) { clearInterval(trafficTimer); trafficTimer = null } })
useVisibleRefresh(refreshTraffic)
</script>

<template>
  <div class="grid grid-cols-1 lg:grid-cols-[360px_minmax(0,1fr)] gap-6 max-w-[1600px] mx-auto">
    <div class="space-y-6">
      <UCard>
        <template #header>
          <div class="font-semibold">
            Клиенты <span class="text-(--ui-text-muted) text-xs ml-1">{{ clients.length }}</span>
          </div>
        </template>

        <div class="space-y-3">
          <UInput
            v-model="search"
            placeholder="Поиск..."
            icon="i-lucide-search"
            size="sm"
            class="w-full"
          />
          <ClientsAddInline />
          <div v-if="filtered.length === 0" class="text-(--ui-text-muted) text-sm py-4 text-center">
            {{ search ? 'Ничего не найдено' : 'Пока нет клиентов' }}
          </div>
          <!-- список расширяется до ~5 карточек, дальше — скролл -->
          <div v-else class="space-y-2 max-h-[480px] overflow-y-auto pr-1">
            <ClientsCard
              v-for="client in filtered"
              :key="client.id"
              :client="client"
              :traffic="trafficMap?.[client.id] ?? null"
              @open="openClient"
            />
          </div>
        </div>
      </UCard>

      <ClientsClientModal
        v-model:open="modalOpen"
        :client-id="selectedClientId"
        @changed="refreshClients"
      />

      <BotsSection />
    </div>

    <div class="space-y-6">
      <RoutesSection />

      <MonitoringSection />

      <ClientsTop @open="openClient" />

      <MonitoringDns />
    </div>
  </div>
</template>
