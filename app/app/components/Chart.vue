<script setup lang="ts">
// График панели: сглаженная линия с заливкой по `data`, второй ряд `extra`
// красным (от нуля), полоса пика `band`; с `tip` наведение показывает значение.
const props = withDefaults(defineProps<{
  data: number[]
  extra?: number[]
  col?: string
  h?: number
  band?: [number, number] | null
  tip?: (i: number) => string
}>(), { extra: undefined, col: 'var(--acc)', h: 60, band: null, tip: undefined })

const W = 400, PAD = 4
const id = useId()
const n = computed(() => props.data.length)
const X = (i: number) => n.value > 1 ? i * W / (n.value - 1) : W / 2

function path(v: number[]) {
  const all = [...props.data, ...(props.extra ?? [])]
  const hi = Math.max(...all), lo = props.extra ? 0 : Math.min(...all)
  const pts = v.map((x, i) => [X(i), PAD + (props.h - 2 * PAD) * (1 - (x - lo) / (hi - lo || 1))] as const)
  let d = `M${pts[0]![0]},${pts[0]![1]}`
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] ?? pts[i]!, p1 = pts[i]!, p2 = pts[i + 1]!, p3 = pts[i + 2] ?? p2
    d += ` C${p1[0] + (p2[0] - p0[0]) / 6},${p1[1] + (p2[1] - p0[1]) / 6} ${p2[0] - (p3[0] - p1[0]) / 6},${p2[1] - (p3[1] - p1[1]) / 6} ${p2[0]},${p2[1]}`
  }
  return d
}
const lines = computed(() => n.value < 2
  ? []
  : [{ d: path(props.data), c: props.col }, ...(props.extra ? [{ d: path(props.extra), c: 'var(--red)' }] : [])])

const at = ref(-1)
function pick(e: PointerEvent) {
  const r = (e.currentTarget as HTMLElement).getBoundingClientRect()
  at.value = n.value > 1 ? Math.round(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * (n.value - 1)) : -1
}
</script>

<template>
  <div class="relative" :style="{ height: `${h}px` }" @pointermove="pick" @pointerleave="at = -1">
    <svg :viewBox="`0 0 ${W} ${h}`" preserveAspectRatio="none" class="block size-full overflow-visible" aria-hidden="true">
      <defs>
        <linearGradient v-for="(s, k) in lines" :id="`${id}${k}`" :key="k" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" :style="{ stopColor: s.c }" stop-opacity=".14" />
          <stop offset="1" :style="{ stopColor: s.c }" stop-opacity="0" />
        </linearGradient>
      </defs>
      <rect v-if="band && n > 1" :x="X(band[0])" :width="X(band[1]) - X(band[0])" :height="h" style="fill:rgba(var(--fg),.05)" />
      <template v-for="(s, k) in lines" :key="k">
        <path :d="`${s.d} L${W},${h} L0,${h} Z`" :fill="`url(#${id}${k})`" />
        <path :d="s.d" fill="none" :style="{ stroke: s.c }" stroke-width="1.5" vector-effect="non-scaling-stroke" />
      </template>
    </svg>
    <template v-if="tip && at >= 0">
      <span class="pointer-events-none absolute inset-y-0 w-px bg-(--line2)" :style="{ left: `${X(at) / W * 100}%` }" />
      <span
        class="pointer-events-none absolute top-0 z-10 rounded-lg border border-(--line) bg-(--tip) px-2 py-1 text-xs whitespace-nowrap"
        :class="at < n / 2 ? 'ml-2' : '-ml-2 -translate-x-full'" :style="{ left: `${X(at) / W * 100}%` }">{{ tip(at) }}</span>
    </template>
  </div>
</template>
