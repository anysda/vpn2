<script setup lang="ts">
// График запросов сводки: все и заблокированные, линейка с подсказкой
// ведётся курсором, без курсора подсказка стоит на пике.
const props = defineProps<{ q: number[], b: number[], units: 'hours' | 'days' }>()

const W = 800, H = 200
const n = computed(() => props.q.length)
const mx = computed(() => Math.max(1, ...props.q) * 1.08)
const X = (i: number) => n.value > 1 ? i * W / (n.value - 1) : W / 2
const Y = (v: number) => H - 6 - (H - 14) * v / mx.value
const qd = computed(() => smooth(props.q.map((v, i) => [X(i), Y(v)])))
const bd = computed(() => smooth(props.b.map((v, i) => [X(i), Y(v)])))
const peak = computed(() => props.q.reduce((m, v, i, a) => v > a[m]! ? i : m, 0))

const now = new Date()
const two = (x: number) => String(x).padStart(2, '0')
const hourOf = (i: number) => (now.getHours() - (n.value - 1 - i) + 24 * 4) % 24
const dayOf = (i: number) => new Date(now.getFullYear(), now.getMonth(), now.getDate() - (n.value - 1 - i))
const ddmm = (d: Date) => `${two(d.getDate())}.${two(d.getMonth() + 1)}`

const span = (i: number) => props.units === 'hours'
  ? `${two(hourOf(i))}:00–${two((hourOf(i) + 1) % 24)}:00`
  : ddmm(dayOf(i))

const marks = computed(() => {
  if (props.units === 'hours') {
    const h = now.getHours()
    return [...Array.from({ length: 6 }, (_, k) => `${two((h + 1 + 4 * k) % 24)}:00`), 'сейчас']
  }
  return [...Array.from({ length: 6 }, (_, k) => ddmm(dayOf(Math.round(k * (n.value - 1) / 6)))), 'сегодня']
})

const plot = ref<HTMLElement>()
const size = ref({ w: 0, h: 0 })
const at = ref(-1)
const hover = ref(false)
const instant = ref(true)
const idx = computed(() => at.value < 0 ? peak.value : at.value)
const pos = computed(() => {
  const i = idx.value
  return { x: n.value > 1 ? i / (n.value - 1) * size.value.w : size.value.w / 2, y: Y(props.q[i] ?? 0) / H * size.value.h }
})

function pick(e: PointerEvent) {
  const r = plot.value!.getBoundingClientRect()
  at.value = Math.max(0, Math.min(n.value - 1, Math.round((e.clientX - r.left) / r.width * (n.value - 1))))
}
function enter(e: PointerEvent) {
  instant.value = !hover.value
  hover.value = true
  pick(e)
  requestAnimationFrame(() => { instant.value = false })
}
function leave() {
  hover.value = false
  at.value = -1
}

let ro: ResizeObserver | undefined
onMounted(() => {
  ro = new ResizeObserver(([e]) => {
    size.value = { w: e!.contentRect.width, h: e!.contentRect.height }
  })
  ro.observe(plot.value!)
  requestAnimationFrame(() => { instant.value = false })
})
onUnmounted(() => ro?.disconnect())
</script>

<template>
  <div class="card qchart">
    <div class="fhead">
      <h3>{{ units === 'hours' ? 'Запросы за сутки' : `Запросы за ${n} дн.` }}</h3>
      <div class="lg">
        <span><span class="dot" style="background:var(--acc)" />Все запросы</span>
        <span><span class="dot" style="background:var(--red)" />Заблокированные</span>
      </div>
    </div>
    <div
      ref="plot"
      class="plot"
      :class="{ hv: hover }"
      @pointerenter="enter"
      @pointermove="pick"
      @pointerleave="leave"
    >
      <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <linearGradient id="dns-qg" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" style="stop-color:var(--acc)" stop-opacity=".12" />
            <stop offset="1" style="stop-color:var(--acc)" stop-opacity="0" />
          </linearGradient>
          <linearGradient id="dns-rg" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" style="stop-color:var(--red)" stop-opacity=".28" />
            <stop offset="1" style="stop-color:var(--red)" stop-opacity="0" />
          </linearGradient>
        </defs>
        <path :d="`${qd} L${W},${H} L0,${H} Z`" fill="url(#dns-qg)" />
        <path :d="qd" fill="none" style="stroke:var(--acc)" stroke-width="1.5" vector-effect="non-scaling-stroke" />
        <path :d="`${bd} L${W},${H} L0,${H} Z`" fill="url(#dns-rg)" />
        <path :d="bd" fill="none" style="stroke:var(--red)" stroke-width="1.5" vector-effect="non-scaling-stroke" />
      </svg>
      <span class="rul" :class="{ now: instant }" :style="{ transform: `translateX(${pos.x}px)` }" />
      <span
        v-if="n && size.w"
        class="qtip"
        :class="{ now: instant, r: idx < n * 0.3 }"
        :style="{ left: 0, top: 0, transform: `translate(${pos.x}px,${pos.y}px)` }"
      >
        <i /><div><b>{{ grp(q[idx] ?? 0) }}</b> запросов · <em>{{ grp(b[idx] ?? 0) }}</em> заблок.<small>{{ span(idx) }}</small></div>
      </span>
    </div>
    <div class="hrs">
      <span v-for="(m, k) in marks" :key="k">{{ m }}</span>
    </div>
  </div>
</template>
