<script setup lang="ts">
import { useClients } from '~/composables/useClients'

useHead({ title: 'anysda-vpn2' })

const { clients, status, refresh } = useClients()
const search = ref('')

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return clients.value
  return clients.value.filter(c => c.name.toLowerCase().includes(q))
})
</script>

<template>
  <div class="grid grid-cols-1 lg:grid-cols-[360px_minmax(0,1fr)] gap-6 max-w-[1600px] mx-auto">
    <div class="space-y-6">
      <UCard>
        <template #header>
          <div class="flex items-center justify-between">
            <div class="font-semibold">
              Клиенты <span class="text-(--ui-text-muted) text-xs ml-1">{{ clients.length }}</span>
            </div>
            <UButton
              icon="i-lucide-refresh-cw"
              size="xs"
              color="neutral"
              variant="ghost"
              :loading="status === 'pending'"
              @click="refresh"
            />
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
            />
          </div>
        </div>
      </UCard>

      <UCard>
        <template #header>
          <div class="font-semibold">
            Боты
          </div>
        </template>
        <div class="text-(--ui-text-muted) text-sm py-4 text-center">
          (Phase D.4 — Telegram настройки)
        </div>
      </UCard>
    </div>

    <div class="space-y-6">
      <RoutesSection />

      <UCard>
        <template #header>
          <div class="font-semibold">
            Мониторинг
          </div>
        </template>
        <div class="text-(--ui-text-muted) text-sm py-4 text-center">
          (Phase D.2 — карточки нод + RTT/Трафик графики)
        </div>
      </UCard>

      <UCard>
        <template #header>
          <div class="font-semibold">
            DNS
          </div>
        </template>
        <div class="text-(--ui-text-muted) text-sm py-4 text-center">
          (Phase D.3 — AdGuard мини-панель)
        </div>
      </UCard>
    </div>
  </div>
</template>
