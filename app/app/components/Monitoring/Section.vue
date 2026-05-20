<script setup lang="ts">
import { flagFor } from '~/composables/useRoutes'
import {
  formatMbps,
  formatPercent,
  formatUptime,
  nodeState,
  sparklinePath,
  useMonitoring,
} from '~/composables/useMonitoring'

const { nodes, cpuHistory } = useMonitoring()

const sortedNodes = computed(() =>
  [...nodes.value]
    .sort((a, b) => {
      if (a.tag === 'ru') return -1
      if (b.tag === 'ru') return 1
      return a.tag.localeCompare(b.tag)
    })
    .map(n => ({ ...n, state: nodeState(n.staleSec) })),
)

function loadColor(v: number | null): string {
  if (v == null) return 'text-(--ui-text-dimmed)'
  if (v >= 80) return 'text-rose-400'
  if (v >= 60) return 'text-amber-400'
  return 'text-(--ui-text)'
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
      class="grid gap-2"
      :style="`grid-template-columns: repeat(${sortedNodes.length}, minmax(120px, 1fr))`"
    >
      <div
        v-for="n in sortedNodes"
        :key="n.tag"
        class="relative rounded-md border border-(--ui-border) bg-(--ui-page) p-2.5"
        :class="{ 'node-warn': n.state === 'warning' }"
      >
        <!-- warning: метрики устарели ≥15с (нода потеряла связь, трафик уже
             увёл watchdog) — весь текст карточки красный. offline ≥3мин. -->
        <div :class="n.state === 'offline' ? 'opacity-30' : ''">
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
          v-if="n.state === 'offline'"
          class="absolute inset-0 flex items-center justify-center pointer-events-none"
        >
          <span class="font-black text-red-600 text-xl tracking-wider">ОФФЛАЙН</span>
        </div>
      </div>
    </div>
  </UCard>
</template>

<style scoped>
/* warning-нода: весь текст и спарклайн карточки — красные */
.node-warn,
.node-warn :deep(*) {
  color: rgb(239 68 68) !important;
}
</style>
