<script setup lang="ts">
import { formatBytes, formatMbps } from '~/composables/useMonitoring'
import type { BucketUnit, TrafficBucket } from '~/composables/useMonitoring'

// Трафик ноды за период: на каждый час (сутки, вчера) или сутки (неделя) два столбца — пик скорости (большее из
// ↓/↑: загрузка канала в нагруженную сторону) и объём (↓+↑). Ряды разных
// единиц, поэтому каждый масштабируется к своему максимуму за окно. Цвет —
// через currentColor, чтобы warning-карточка (.node-warn) красила и диаграмму.
const props = defineProps<{ buckets: TrafficBucket[], unit: BucketUnit }>()

const GROUP_W = 10
const BAR_W = 3.6
const HEIGHT = 32
// Ненулевое значение всегда видно, даже рядом с большим пиком.
const MIN_BAR = 0.8

const sum = (a: number | null, b: number | null) => (a == null && b == null ? null : (a ?? 0) + (b ?? 0))
const maxOf = (a: number | null, b: number | null) => (a == null && b == null ? null : Math.max(a ?? 0, b ?? 0))

const bars = computed(() => {
  const rows = props.buckets.map(h => ({ h, peak: maxOf(h.rxPeakBps, h.txPeakBps), vol: sum(h.rxBytes, h.txBytes) }))
  const maxPeak = Math.max(1, ...rows.map(r => r.peak ?? 0))
  const maxVol = Math.max(1, ...rows.map(r => r.vol ?? 0))
  const height = (v: number | null, max: number) => (v == null || v <= 0 ? 0 : Math.max(MIN_BAR, (v / max) * HEIGHT))
  return rows.map((r, i) => ({
    ...r,
    x: i * GROUP_W,
    peakH: height(r.peak, maxPeak),
    volH: height(r.vol, maxVol),
  }))
})

const width = computed(() => props.buckets.length * GROUP_W)
const active = ref<number | null>(null)
const activeBar = computed(() => (active.value == null ? null : bars.value[active.value] ?? null))

// Время МСК (UTC+3 без перехода на летнее время).
const msk = (sec: number) => new Date((sec + 3 * 3600) * 1000)
const pad = (n: number) => String(n).padStart(2, '0')
const hhmm = (sec: number) => `${pad(msk(sec).getUTCHours())}:00`
const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб']
const ddmm = (sec: number) => `${pad(msk(sec).getUTCDate())}.${pad(msk(sec).getUTCMonth() + 1)}`
const bucketTitle = (b: TrafficBucket) => (props.unit === 'day'
  ? `${WEEKDAYS[msk(b.start).getUTCDay()]} ${ddmm(b.start)}, МСК`
  : `${hhmm(b.start)}–${b.end % 86400 === 21 * 3600 ? '24:00' : hhmm(b.end)} МСК`)
// Подписи под диаграммой: начало и конец сетки.
const firstLabel = computed(() => {
  const b = props.buckets[0]
  return b ? (props.unit === 'day' ? ddmm(b.start) : hhmm(b.start)) : ''
})
const lastLabel = computed(() => {
  const b = props.buckets.at(-1)
  if (!b) return ''
  if (props.unit === 'day') return b.current ? 'сегодня' : ddmm(b.start)
  return '24:00'
})
const unitWord = computed(() => (props.unit === 'day' ? 'сутки' : 'час'))
const toMbps = (bps: number | null) => (bps == null ? null : (bps * 8) / 1e6)

// Подсказка не должна вылезать за карточку: у краёв прижимаем к краю.
const tipStyle = computed(() => {
  if (active.value == null) return {}
  const n = props.buckets.length
  const center = ((active.value + 0.5) / n) * 100
  if (active.value < n / 3) return { left: '0' }
  if (active.value >= (2 * n) / 3) return { right: '0' }
  return { left: `${center}%`, transform: 'translateX(-50%)' }
})
</script>

<template>
  <div
    class="relative"
    data-testid="node-hourly"
    @mouseleave="active = null"
  >
    <svg
      :viewBox="`0 0 ${width} ${HEIGHT}`"
      class="w-full h-8 block"
      preserveAspectRatio="none"
    >
      <g
        v-for="(b, i) in bars"
        :key="b.h.start"
        :opacity="b.h.current ? 0.55 : 1"
      >
        <rect
          v-if="active === i"
          :x="b.x"
          y="0"
          :width="GROUP_W"
          :height="HEIGHT"
          fill="currentColor"
          class="text-(--ui-text-muted)"
          opacity="0.15"
        />
        <!-- будущий час — пусто; нет данных — серая риска вместо столбцов -->
        <template v-if="b.h.future" />
        <rect
          v-else-if="b.peak == null && b.vol == null"
          :x="b.x + 1"
          :y="HEIGHT - 0.8"
          :width="GROUP_W - 2"
          height="0.8"
          fill="currentColor"
          class="text-(--ui-text-dimmed)"
        />
        <template v-else>
          <rect
            :x="b.x + 1"
            :y="HEIGHT - b.peakH"
            :width="BAR_W"
            :height="b.peakH"
            fill="currentColor"
            class="text-violet-500"
          />
          <rect
            :x="b.x + 1 + BAR_W + 0.6"
            :y="HEIGHT - b.volH"
            :width="BAR_W"
            :height="b.volH"
            fill="currentColor"
            class="text-sky-400"
          />
        </template>
        <rect
          :x="b.x"
          y="0"
          :width="GROUP_W"
          :height="HEIGHT"
          fill="transparent"
          @mouseenter="active = i"
          @click="active = active === i ? null : i"
        />
      </g>
    </svg>
    <div class="flex items-center justify-between text-[10px] leading-3 mt-0.5 text-(--ui-text-dimmed)">
      <span>{{ firstLabel }}</span>
      <span class="flex items-center gap-1.5">
        <span class="flex items-center gap-0.5"><span class="inline-block size-1.5 rounded-full bg-violet-500" />пик</span>
        <span class="flex items-center gap-0.5"><span class="inline-block size-1.5 rounded-full bg-sky-400" />объём</span>
      </span>
      <span>{{ lastLabel }}</span>
    </div>
    <div
      v-if="activeBar"
      class="absolute bottom-full mb-1 z-20 w-max max-w-60 rounded-md border border-(--ui-border) bg-(--ui-bg-elevated) px-2 py-1.5 text-[11px] leading-4 shadow-lg pointer-events-none"
      :style="tipStyle"
    >
      <div class="font-semibold">
        {{ bucketTitle(activeBar.h) }}
        <span
          v-if="activeBar.h.current"
          class="font-normal text-(--ui-text-muted)"
        >· {{ unit === 'day' ? 'сегодня, неполные' : 'текущий, неполный' }}</span>
      </div>
      <div
        v-if="activeBar.h.future"
        class="text-(--ui-text-muted)"
      >
        ещё не наступил
      </div>
      <div
        v-else-if="activeBar.peak == null && activeBar.vol == null"
        class="text-(--ui-text-muted)"
      >
        нет данных (нода недоступна или вне хранения метрик)
      </div>
      <template v-else>
        <div>
          <span class="inline-block size-1.5 rounded-full bg-violet-500 mr-1" />пик скорости:
          ↓ {{ formatMbps(toMbps(activeBar.h.rxPeakBps)) }} ↑ {{ formatMbps(toMbps(activeBar.h.txPeakBps)) }} Мбит/с
        </div>
        <div>
          <span class="inline-block size-1.5 rounded-full bg-sky-400 mr-1" />объём:
          ↓ {{ formatBytes(activeBar.h.rxBytes) }} ↑ {{ formatBytes(activeBar.h.txBytes) }}
        </div>
      </template>
      <div class="text-[10px] text-(--ui-text-dimmed) mt-0.5">
        пик — макс. средняя за минуту внутри {{ unit === 'day' ? 'суток' : 'часа' }}; столбцы — каждый к своему максимуму за период ({{ buckets.length }} × {{ unitWord }})
      </div>
    </div>
  </div>
</template>
