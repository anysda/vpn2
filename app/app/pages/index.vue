<script setup lang="ts">
import { useClients } from '~/composables/useClients'
import type { ClientTraffic } from '~/composables/useClients'
import type { Outbound } from '~/composables/useRoutes'
import { flagFor, parseOutbound } from '~/composables/useRoutes'

useHead({ title: 'anysda-vpn2' })

const { clients, refresh: refreshClients, update: updateClient } = useClients()
const toast = useToast()

// Экзиты для выпадашки «предпочитаемый экзит» в карточках (direct-выходы, без RU).
const { data: outboundList } = useFetch<Outbound[]>('/api/routes/outbounds', {
  default: () => [],
  server: false,
})
const exitTags = computed(() => [...new Set(
  outboundList.value
    .map(o => parseOutbound(o.name))
    .filter(p => p && p.variant === 'direct' && p.tag !== 'ru')
    .map(p => p!.tag),
)].sort())

async function setPreferredExit(id: number, tag: string | null) {
  try {
    await updateClient(id, { preferredExit: tag })
    toast.add({
      title: tag ? `Предпочитаемый экзит: ${flagFor(tag)} ${tag.toUpperCase()}` : 'Предпочитаемый экзит: авто',
      color: 'success',
    })
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка', color: 'error' })
  }
}
const search = ref('')

// Сортировка на лету (опрос трафика раз в 3 с): текущая скорость → трафик за
// сутки → трафик за всё время → имя.
const sorted = computed(() => {
  const q = search.value.trim().toLowerCase()
  const list = q ? clients.value.filter(c => c.name.toLowerCase().includes(q)) : [...clients.value]
  const t = trafficMap.value ?? {}
  const speed = (id: number) => (t[id]?.online ? (t[id]?.rxBps ?? 0) + (t[id]?.txBps ?? 0) : 0)
  const day = (id: number) => (t[id]?.dayRx ?? 0) + (t[id]?.dayTx ?? 0)
  const total = (id: number) => (t[id]?.rxBytes ?? 0) + (t[id]?.txBytes ?? 0)
  return list.sort((a, b) =>
    speed(b.id) - speed(a.id)
    || day(b.id) - day(a.id)
    || total(b.id) - total(a.id)
    || a.name.localeCompare(b.name, 'ru'),
  )
})

// Пока курсор мыши над списком, порядок замораживаем (цифры обновляются):
// иначе карточка уезжает из-под курсора и клик попадает в соседнего клиента.
// Новые клиенты — в конец, удалённые исчезают; после ухода курсора — пересортировка.
// Только мышь: на тач-экране pointerleave может не прийти, и список застыл бы.
const listHovered = ref(false)
const frozenOrder = ref<number[] | null>(null)
watch(listHovered, (h) => {
  frozenOrder.value = h ? sorted.value.map(c => c.id) : null
})
const filtered = computed(() => {
  if (!frozenOrder.value) return sorted.value
  const byId = new Map(sorted.value.map(c => [c.id, c]))
  const kept = frozenOrder.value.filter(id => byId.has(id)).map(id => byId.get(id)!)
  const seen = new Set(frozenOrder.value)
  return [...kept, ...sorted.value.filter(c => !seen.has(c.id))]
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
          <!-- список расширяется до ~5 карточек, дальше — скролл.
               overflow-anchor: none — карточки переставляются каждые 3 с, и
               scroll anchoring браузера, цепляясь за переехавшую карточку,
               подкручивал прокрутку (страница «прыгала»). -->
          <div
            v-else
            class="space-y-2 max-h-[480px] overflow-y-auto pr-1 [overflow-anchor:none]"
            @pointerenter="(e: PointerEvent) => { if (e.pointerType === 'mouse') listHovered = true }"
            @pointerleave="listHovered = false"
          >
            <ClientsCard
              v-for="client in filtered"
              :key="client.id"
              :client="client"
              :traffic="trafficMap?.[client.id] ?? null"
              :exits="exitTags"
              @open="openClient"
              @prefer="setPreferredExit"
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
