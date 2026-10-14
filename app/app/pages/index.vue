<script setup lang="ts">
import { useClients } from '~/composables/useClients'
import { useExitTags } from '~/composables/useRoutes'
import { useScreenLayout } from '~/composables/useScreenLayout'

useHead({ title: 'Обзор — anysda-vpn2' })

const { refresh: refreshClients } = useClients()
const { layout } = useScreenLayout()
const exitTags = useExitTags()

// Остальные модули переехали в свои разделы; здесь — то, что относится к обзору.
const OVERVIEW = new Set(['monitoring', 'top'])
const modules = computed(() => [...layout.value.left, ...layout.value.right].filter(id => OVERVIEW.has(id)))

// Модалка клиента, открывается кликом по строке ТОПа.
const modalOpen = ref(false)
const selectedClientId = ref<number | null>(null)
function openClient(id: number) {
  selectedClientId.value = id
  modalOpen.value = true
}
</script>

<template>
  <main class="tab on">
    <div class="grid gap-3">
      <div v-for="id in modules" :key="id">
        <MonitoringSection v-if="id === 'monitoring'" />
        <ClientsTop v-else-if="id === 'top'" @open="openClient" />
      </div>
      <EventsFeed />
    </div>

    <ClientsClientModal
      v-model:open="modalOpen"
      :client-id="selectedClientId"
      :exits="exitTags"
      @changed="refreshClients"
    />
  </main>
</template>
