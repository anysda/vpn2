<script setup lang="ts">
import { flagFor, rttColor } from '~/composables/useRoutes'
import {
  formatMbps,
  formatPercent,
  formatUptime,
  sparklinePath,
  useMonitoring,
} from '~/composables/useMonitoring'

const { nodes, outbounds, cpuHistory } = useMonitoring()

function colorForCpu(cpu: number | null): string {
  if (cpu == null) return 'text-(--ui-text-muted)'
  if (cpu >= 80) return 'text-red-500'
  if (cpu >= 60) return 'text-yellow-500'
  return 'text-(--ui-text)'
}

function colorForRam(ram: number | null): string {
  if (ram == null) return 'text-(--ui-text-muted)'
  if (ram >= 80) return 'text-red-500'
  if (ram >= 60) return 'text-yellow-500'
  return 'text-(--ui-text)'
}

function flagForNode(tag: string): string {
  if (tag === 'ru') return '🇷🇺'
  return flagFor(`hy2-${tag}-direct`)
}
</script>

<template>
  <UCard>
    <template #header>
      <div class="flex items-center justify-between">
        <div class="font-semibold">
          Мониторинг
        </div>
        <span class="text-xs text-(--ui-text-muted) flex items-center gap-1">
          <UIcon name="i-lucide-radio" class="text-emerald-500" />
          лайв · 2с
        </span>
      </div>
    </template>

    <div class="space-y-4">
      <!-- Nodes -->
      <div
        v-if="nodes.length > 0"
        class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2"
      >
        <div
          v-for="n in nodes"
          :key="n.tag"
          class="border border-(--ui-border) rounded-md p-2"
        >
          <div class="flex items-center justify-between text-xs mb-1">
            <span class="font-medium">{{ flagForNode(n.tag) }} {{ n.label }}</span>
            <span class="text-(--ui-text-muted)">{{ formatUptime(n.uptimeSec) }}</span>
          </div>
          <div class="grid grid-cols-2 gap-x-2 text-xs">
            <span class="text-(--ui-text-muted)">CPU</span>
            <span :class="colorForCpu(n.cpu)" class="text-right">{{ formatPercent(n.cpu) }}</span>
            <span class="text-(--ui-text-muted)">RAM</span>
            <span :class="colorForRam(n.ram)" class="text-right">{{ formatPercent(n.ram) }}</span>
            <span class="text-(--ui-text-muted)">↓</span>
            <span class="text-right">{{ formatMbps(n.rxMbps) }} Mbps</span>
            <span class="text-(--ui-text-muted)">↑</span>
            <span class="text-right">{{ formatMbps(n.txMbps) }} Mbps</span>
          </div>
          <svg
            v-if="(cpuHistory.get(n.tag) ?? []).length > 1"
            viewBox="0 0 80 24"
            class="w-full h-6 mt-1"
            preserveAspectRatio="none"
          >
            <path
              :d="sparklinePath(cpuHistory.get(n.tag) ?? [])"
              fill="none"
              stroke="currentColor"
              stroke-width="1.2"
              class="text-(--ui-primary) opacity-70"
            />
          </svg>
        </div>
      </div>

      <div v-else class="text-(--ui-text-muted) text-sm text-center py-4">
        Загружаю метрики…
      </div>

      <!-- Outbounds -->
      <div v-if="outbounds.length > 0">
        <div class="text-xs font-medium text-(--ui-text-muted) mb-2">
          Outbounds (RTT · трафик)
        </div>
        <div class="space-y-1 text-xs">
          <div
            v-for="ob in outbounds"
            :key="ob.name"
            class="flex items-center justify-between border border-(--ui-border) rounded px-2 py-1.5"
          >
            <div class="flex items-center gap-2 min-w-0">
              <UIcon
                v-if="ob.isWarp"
                name="i-lucide-zap"
                class="text-yellow-500"
              />
              <UIcon
                v-else
                name="i-lucide-circle"
                :class="`text-${rttColor(ob.rttMs)}-500`"
              />
              <span class="truncate font-medium">{{ flagFor(ob.name) }} {{ ob.name }}</span>
            </div>
            <div class="flex items-center gap-4 text-(--ui-text-muted) shrink-0">
              <span>{{ ob.rttMs == null ? '—' : `${ob.rttMs}ms` }}</span>
              <span>↓ {{ ob.downKbps.toFixed(1) }}</span>
              <span>↑ {{ ob.upKbps.toFixed(1) }} kbps</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </UCard>
</template>
