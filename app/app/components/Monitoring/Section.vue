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

function loadColor(v: number | null): string {
  if (v == null) return 'text-zinc-500'
  if (v >= 80) return 'text-rose-400'
  if (v >= 60) return 'text-amber-400'
  return 'text-zinc-200'
}
</script>

<template>
  <UCard>
    <template #header>
      <div class="flex items-center justify-between">
        <div class="font-semibold">
          Мониторинг
        </div>
        <span class="text-xs text-zinc-500 flex items-center gap-1">
          <span class="size-2 rounded-full bg-emerald-500 animate-pulse" />
          live
        </span>
      </div>
    </template>

    <div
      v-if="nodes.length === 0"
      class="text-zinc-500 text-sm text-center py-6"
    >
      загружаю метрики…
    </div>
    <div
      v-else
      class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-2"
    >
      <div
        v-for="n in nodes"
        :key="n.tag"
        class="rounded-md border border-zinc-800 bg-zinc-900/50 p-2.5"
      >
        <div class="flex items-center justify-between text-xs mb-2">
          <span class="font-semibold">{{ flagFor(n.tag) }} {{ n.tag.toUpperCase() }}</span>
          <span class="text-zinc-500">{{ formatUptime(n.uptimeSec) }}</span>
        </div>
        <div class="space-y-0.5 text-xs">
          <div class="flex justify-between">
            <span class="text-zinc-500">CPU</span>
            <span :class="loadColor(n.cpu)">{{ formatPercent(n.cpu) }}</span>
          </div>
          <div class="flex justify-between">
            <span class="text-zinc-500">RAM</span>
            <span :class="loadColor(n.ram)">{{ formatPercent(n.ram) }}</span>
          </div>
          <div class="flex justify-between">
            <span class="text-zinc-500">↓</span>
            <span class="text-zinc-200">{{ formatMbps(n.rxMbps) }} <span class="text-zinc-500">Mbps</span></span>
          </div>
          <div class="flex justify-between">
            <span class="text-zinc-500">↑</span>
            <span class="text-zinc-200">{{ formatMbps(n.txMbps) }} <span class="text-zinc-500">Mbps</span></span>
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
            class="text-emerald-400/70"
          />
        </svg>
      </div>
    </div>
  </UCard>
</template>
