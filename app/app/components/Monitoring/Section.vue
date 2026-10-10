<script setup lang="ts">
import { PERIOD_META, useTrafficPeriod } from '~/composables/useClients'
import { flagFor } from '~/composables/useRoutes'
import {
  formatBytes,
  formatMbps,
  formatPercent,
  formatUptime,
  nodeState,
  useMonitoring,
} from '~/composables/useMonitoring'
import type { NodeMetric } from '~/composables/useMonitoring'

const { nodes, hourly, bucketUnit, setExitDisabled } = useMonitoring()
// Период трафика нод — общий с селектором «ТОП клиентов».
const period = useTrafficPeriod()
const periodMeta = computed(() => PERIOD_META[period.value])
const toast = useToast()
const toggling = ref<string | null>(null)

const ERRORS: Record<string, string> = {
  last_enabled_exit: 'Нельзя выключить последний включённый экзит',
  unknown_exit: 'Неизвестный экзит',
}

async function toggleExit(n: NodeMetric, enabled: boolean) {
  const disabled = !enabled
  if (disabled && (n.active || n.activeDevices)
    && !confirm(`Через ${n.tag.toUpperCase()} сейчас идёт трафик. Выключить? Watchdog уведёт его на другие экзиты, текущие соединения оборвутся.`)) {
    return
  }
  toggling.value = n.tag
  try {
    await setExitDisabled(n.tag, disabled)
    toast.add({ title: `${n.tag.toUpperCase()} ${disabled ? 'выключен' : 'включён'}`, color: 'success' })
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: ERRORS[err.statusMessage ?? ''] ?? err.statusMessage ?? 'Ошибка', color: 'error' })
  }
  finally {
    toggling.value = null
  }
}

const sortedNodes = computed(() =>
  [...nodes.value]
    .sort((a, b) => {
      if (a.tag === 'ru') return -1
      if (b.tag === 'ru') return 1
      return a.tag.localeCompare(b.tag)
    })
    .map(n => ({ ...n, state: nodeState(n.staleSec) })),
)
const hasLanes = computed(() => nodes.value.some(n => n.lanes != null))

function loadColor(v: number | null): string {
  if (v == null) return 'text-(--ui-text-dimmed)'
  if (v >= 80) return 'text-rose-400'
  if (v >= 60) return 'text-amber-400'
  return 'text-(--ui-text)'
}

// Свободное место: меньше 20% — пора чистить, меньше 10% — горит.
function diskColor(freePct: number | null): string {
  if (freePct == null) return 'text-(--ui-text-dimmed)'
  if (freePct < 10) return 'text-rose-400'
  if (freePct < 20) return 'text-amber-400'
  return 'text-(--ui-text)'
}

function formatDiskFree(bytes: number | null): string {
  return bytes == null ? '—' : `${(bytes / 1024 ** 3).toFixed(1)} GB`
}

// iowait/steal: уже 10% — заметная беда (диск или соседи по гипервизору).
function waitColor(v: number | null): string {
  if (v == null) return 'text-(--ui-text-dimmed)'
  if (v >= 30) return 'text-rose-400'
  if (v >= 10) return 'text-amber-400'
  return 'text-(--ui-text-muted)'
}
</script>

<template>
  <UCard>
    <template #header>
      <div class="flex items-baseline justify-between gap-2">
        <div class="font-semibold">
          Мониторинг
        </div>
        <div class="text-xs text-(--ui-text-muted)">
          трафик за период: {{ periodMeta.short }} — {{ periodMeta.note }}
        </div>
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
      style="grid-template-columns: repeat(auto-fit, minmax(132px, 1fr))"
    >
      <div
        v-for="n in sortedNodes"
        :key="n.tag"
        class="relative rounded-md border border-(--ui-border) bg-(--ui-page) p-2.5"
        :class="{ 'node-warn': n.state === 'warning' }"
      >
        <!-- warning: метрики устарели ≥5с (нода потеряла связь, трафик уже
             увёл watchdog) — весь текст карточки красный. offline ≥3мин. -->
        <div :class="n.state === 'offline' ? 'opacity-30' : n.disabled ? 'opacity-50' : ''">
          <div class="flex items-center justify-between text-xs mb-1">
            <span class="font-semibold">{{ flagFor(n.tag) }} {{ n.tag.toUpperCase() }}</span>
            <span class="text-(--ui-text-muted)">{{ formatUptime(n.uptimeSec) }}</span>
          </div>
          <div
            v-if="n.host"
            class="text-[11px] font-mono text-(--ui-text-muted) truncate select-all -mt-0.5 mb-1"
            :title="n.host"
          >
            {{ n.host }}
          </div>
          <div
            v-if="n.tag !== 'ru'"
            class="flex items-center justify-between mb-2 min-h-5"
          >
            <UBadge
              v-if="n.disabled"
              color="neutral"
              variant="subtle"
              size="sm"
            >
              ВЫКЛ
            </UBadge>
            <UBadge
              v-else-if="n.activeDevices"
              color="success"
              variant="subtle"
              size="sm"
              title="Устройств с соединениями через этот экзит сейчас"
            >
              {{ n.activeDevices }} устр.
            </UBadge>
            <span v-else />
            <USwitch
              :model-value="!n.disabled"
              :loading="toggling === n.tag"
              :disabled="toggling !== null"
              size="xs"
              :aria-label="`Экзит ${n.tag.toUpperCase()} включён`"
              @update:model-value="(v: boolean) => toggleExit(n, v)"
            />
          </div>
          <!-- у RU выключателя нет — держим ту же высоту, чтобы строки карточек совпадали -->
          <div
            v-else
            class="mb-2 min-h-5"
          />
          <div class="space-y-0.5 text-xs">
            <div class="flex justify-between">
              <span class="text-(--ui-text-muted)">CPU</span>
              <span :class="loadColor(n.cpu)">{{ formatPercent(n.cpu) }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-(--ui-text-muted)">iowait</span>
              <span :class="waitColor(n.iowait)">{{ formatPercent(n.iowait) }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-(--ui-text-muted)">steal</span>
              <span :class="waitColor(n.steal)">{{ formatPercent(n.steal) }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-(--ui-text-muted)">RAM</span>
              <span :class="loadColor(n.ram)">{{ formatPercent(n.ram) }}</span>
            </div>
            <div
              class="flex justify-between gap-1"
              title="Свободно на корневом разделе"
            >
              <span class="text-(--ui-text-muted)">HDD</span>
              <span :class="diskColor(n.diskFreePct)" class="whitespace-nowrap">
                {{ formatDiskFree(n.diskFreeBytes) }}
                <span class="text-(--ui-text-muted)">{{ formatPercent(n.diskFreePct) }}</span>
              </span>
            </div>
            <div class="flex justify-between">
              <span class="text-(--ui-text-muted)">↓</span>
              <span class="text-(--ui-text)">{{ formatMbps(n.rxMbps) }} <span class="text-(--ui-text-muted)">Mbps</span></span>
            </div>
            <div class="flex justify-between">
              <span class="text-(--ui-text-muted)">↑</span>
              <span class="text-(--ui-text)">{{ formatMbps(n.txMbps) }} <span class="text-(--ui-text-muted)">Mbps</span></span>
            </div>
            <div
              v-if="n.lanes != null"
              class="flex justify-between"
              title="Дорожек (lane-NN), направленных на узел; warp — из них через warp узла. Без балансировки все дорожки повторяют основной выход."
            >
              <span class="text-(--ui-text-muted)">полос</span>
              <span class="text-(--ui-text) whitespace-nowrap">{{ n.lanes }} <span class="text-(--ui-text-muted)">(warp {{ n.lanesWarp }})</span></span>
            </div>
            <!-- у RU дорожек нет — пустая строка той же высоты, чтобы итоги за сутки стояли вровень -->
            <div
              v-else-if="hasLanes"
              class="h-4"
            />
            <div class="border-t border-(--ui-border) my-1" />
            <div
              class="flex justify-between"
              :title="`Принято нодой: ${periodMeta.short} (${periodMeta.note})`"
            >
              <span class="text-(--ui-text-muted)">{{ periodMeta.short }} ↓</span>
              <span class="text-(--ui-text)">{{ formatBytes(n.rxPeriodBytes) }}</span>
            </div>
            <div
              class="flex justify-between"
              :title="`Отдано нодой: ${periodMeta.short} (${periodMeta.note})`"
            >
              <span class="text-(--ui-text-muted)">{{ periodMeta.short }} ↑</span>
              <span class="text-(--ui-text)">{{ formatBytes(n.txPeriodBytes) }}</span>
            </div>
          </div>
          <MonitoringHourlyBars
            v-if="hourly.get(n.tag)?.length"
            :buckets="hourly.get(n.tag)!"
            :unit="bucketUnit"
            class="mt-2"
          />
          <!-- место под диаграмму, пока грузится другой период: карточка не прыгает -->
          <div
            v-else
            class="mt-2 h-[46px]"
          />
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
/* warning-нода: весь текст и диаграмма карточки — красные */
.node-warn,
.node-warn :deep(*) {
  color: rgb(239 68 68) !important;
}
</style>
