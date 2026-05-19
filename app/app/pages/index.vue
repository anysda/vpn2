<script setup lang="ts">
import { useClients } from '~/composables/useClients'

useHead({ title: 'anysda-vpn2' })

const { clients } = useClients()
const search = ref('')

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return clients.value
  return clients.value.filter(c => c.name.toLowerCase().includes(q))
})

const { data: trafficMap, refresh: refreshTraffic } = useFetch<Record<number, { rxBytes: number, txBytes: number }>>('/api/clients/traffic', {
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
          <div v-else class="space-y-2">
            <ClientsCard
              v-for="client in filtered"
              :key="client.id"
              :client="client"
              :traffic="trafficMap?.[client.id] ?? null"
            />
          </div>
        </div>
      </UCard>

      <BotsSection />
    </div>

    <div class="space-y-6">
      <RoutesSection />

      <MonitoringSection />

      <MonitoringDns />
    </div>
  </div>
</template>
