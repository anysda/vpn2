<script setup lang="ts">
import { flagFor } from '~/composables/useRoutes'
import {
  formatMbps,
  formatPercent,
  formatUptime,
  sparklinePath,
  useMonitoring,
} from '~/composables/useMonitoring'

const { nodes, cpuHistory } = useMonitoring()

const sortedNodes = computed(() => {
  return [...nodes.value].sort((a, b) => {
    if (a.tag === 'ru') return -1
    if (b.tag === 'ru') return 1
    return a.tag.localeCompare(b.tag)
  })
})

function loadColor(v: number | null): string {
  if (v == null) return 'text-(--ui-text-dimmed)'
  if (v >= 80) return 'text-rose-400'
  if (v >= 60) return 'text-amber-400'
  return 'text-(--ui-text)'
}

function isOffline(n: { cpu: number | null, ram: number | null, uptimeSec: number | null }): boolean {
  return n.cpu == null && n.ram == null && n.uptimeSec == null
}
</script>

<template>
  <UCard>
    <template #header>
      <div class="font-semibold">
        Мониторинг
      </div>
    </template>

    <div
      v-if="sortedNodes.length === 0"
      class="text-(--ui-text-muted) text-sm text-center py-6"
    >
      загружаю метрики…
    </div>
    <div
      v-else
      class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-2"
    >
      <div
        v-for="n in sortedNodes"
        :key="n.tag"
        class="relative rounded-md border border-(--ui-border) bg-(--ui-page) p-2.5"
      >
        <div :class="isOffline(n) ? 'opacity-30' : ''">
          <div class="flex items-center justify-between text-xs mb-2">
            <span class="font-semibold">{{ flagFor(n.tag) }} {{ n.tag.toUpperCase() }}</span>
            <span class="text-(--ui-text-muted)">{{ formatUptime(n.uptimeSec) }}</span>
          </div>
          <div class="space-y-0.5 text-xs">
            <div class="flex justify-between">
              <span class="text-(--ui-text-muted)">CPU</span>
              <span :class="loadColor(n.cpu)">{{ formatPercent(n.cpu) }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-(--ui-text-muted)">RAM</span>
              <span :class="loadColor(n.ram)">{{ formatPercent(n.ram) }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-(--ui-text-muted)">↓</span>
              <span class="text-(--ui-text)">{{ formatMbps(n.rxMbps) }} <span class="text-(--ui-text-muted)">Mbps</span></span>
            </div>
            <div class="flex justify-between">
              <span class="text-(--ui-text-muted)">↑</span>
              <span class="text-(--ui-text)">{{ formatMbps(n.txMbps) }} <span class="text-(--ui-text-muted)">Mbps</span></span>
            </div>
          </div>
          <svg
            v-if="(cpuHistory.get(n.tag) ?? []).length > 1"
            viewBox="0 0 80 16"
            class="w-full h-4 mt-2"
            preserveAspectRatio="none"
          >
            <path
              :d="sparklinePath(cpuHistory.get(n.tag) ?? [], 80, 16)"
              fill="none"
              stroke="currentColor"
              stroke-width="1.2"
              class="text-violet-500/80"
            />
          </svg>
        </div>
        <div
          v-if="isOffline(n)"
          class="absolute inset-0 flex items-center justify-center pointer-events-none"
        >
          <span class="font-black text-red-600 text-xl tracking-wider">ОФФЛАЙН</span>
        </div>
      </div>
    </div>
  </UCard>
</template>
