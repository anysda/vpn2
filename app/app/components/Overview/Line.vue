<script setup lang="ts">
import { anim } from '~/utils/clients-view'
import { smooth } from '~/utils/dns-motion'
import { drawSvg, linePoints, reduced, useWake } from '~/utils/overview'

// Линия макета (line + dotsHTML + liveDot): svg тянется по обёртке, кружки
// и живая точка — html поверх, чтобы не плющились вместе с viewBox.
const props = withDefaults(defineProps<{
  data: number[]
  w: number
  h: number
  pad?: number
  band?: [number, number] | null
  dots?: number[]
  col?: string
  fill?: boolean
  // живая точка на последнем значении: true, 'bad' (красная) или нет
  live?: boolean | 'bad'
  liveIdx?: number
  // [задержка, длительность] прорисовки в сцене пробуждения
  draw?: [number, number]
}>(), { pad: 8, band: null, dots: () => [], col: 'var(--acc)', fill: true, live: false, liveIdx: 0, draw: undefined })

type Pt = [number, number]

const id = useId()
const svg = ref<SVGSVGElement>()
const host = ref<HTMLElement>()
const wake = useWake()

const pts = computed(() => props.data.length > 1 ? linePoints(props.data, props.w, props.h, props.pad) : [])
const bandRect = computed(() => {
  const p = pts.value, b = props.band
  if (!b || !p[b[0]] || !p[b[1]]) return null
  return { x: p[b[0]]![0], width: p[b[1]]![0] - p[b[0]]![0] }
})
const dotPos = computed(() => props.dots
  .filter(i => pts.value[i])
  .map(i => ({ left: `${pts.value[i]![0] / props.w * 100}%`, top: `${pts.value[i]![1] / props.h * 100}%` })))

// Лента: ряд уехал на значение — линия едет влево ровно и без остановок.
// Ушедшие значения остаются слева за рамкой (tail), новое приходит справа
// из-за рамки; off — сколько шагов ещё проехать, тает до нуля со скоростью
// шаг за период опроса. Шкала по высоте догоняет новый min/max плавно.
const tail = ref<number[]>([])
const off = ref(0)
const scale = ref<Pt | null>(null)
const ticking = computed(() => off.value > 0 || scale.value != null)

const range = (v: number[]): Pt => [Math.min(...v), Math.max(...v)]
const step = computed(() => props.w / Math.max(1, props.data.length - 1))
// цель шкалы: всё, что сейчас в рамке, включая уходящее значение слева
const target = computed(() => range([...tail.value.slice(-Math.ceil(off.value)), ...props.data]))

const linePts = computed<Pt[]>(() => {
  if (!ticking.value || props.data.length < 2) return pts.value
  const ext = [...tail.value, ...props.data], k = tail.value.length
  const [mn, mx] = scale.value ?? target.value
  const { h, pad } = props
  return ext.map((v, j) => [(j - k + off.value) * step.value, pad + (h - 2 * pad) * (1 - (v - mn) / (mx - mn || 1))])
})
const d = computed(() => linePts.value.length ? smooth(linePts.value) : '')
const area = computed(() => {
  const p = linePts.value
  return p.length ? `${d.value} L${p.at(-1)![0]},${props.h} L${p[0]![0]},${props.h} Z` : ''
})

/** Высота линии в точке x: тот же сплайн, что рисует smooth, x(t) монотонна. */
function yAt(p: Pt[], x: number): number {
  const i = p.findIndex((q, n) => n < p.length - 1 && p[n + 1]![0] >= x && q[0] <= x)
  if (i < 0) return p.at(-1)![1]
  const p0 = p[i - 1] ?? p[i]!, p1 = p[i]!, p2 = p[i + 1]!, p3 = p[i + 2] ?? p2
  const c1: Pt = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6]
  const c2: Pt = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6]
  const at = (t: number, a: number, b: number, c: number, e: number) => {
    const u = 1 - t
    return u * u * u * a + 3 * u * u * t * b + 3 * u * t * t * c + t * t * t * e
  }
  let lo = 0, hi = 1
  for (let n = 0; n < 24; n++) {
    const m = (lo + hi) / 2
    if (at(m, p1[0], c1[0], c2[0], p2[0]) < x) lo = m
    else hi = m
  }
  return at((lo + hi) / 2, p1[1], c1[1], c2[1], p2[1])
}

// живая точка: px по размеру обёртки, всегда на конце линии у правого края
const size = ref({ w: 0, h: 0 })
const liveOn = ref(false)
const liveEl = ref<HTMLElement>()
const liveStyle = computed(() => {
  const p = linePts.value
  if (!p.length) return {}
  const y = ticking.value ? yAt(p, props.w) : p.at(-1)![1]
  return {
    transform: `translate(${size.value.w - 1}px,${y / props.h * size.value.h}px)`,
    // в ленте точка идёт за линией кадр в кадр, без своей задержки
    transition: ticking.value ? 'none' : undefined,
  }
})

let raf = 0, last = 0, period = 2000, lastShift = 0
function stop() {
  cancelAnimationFrame(raf)
  raf = 0
  tail.value = []
  off.value = 0
  scale.value = null
}
function frame(now: number) {
  const dt = Math.min(100, now - last)
  last = now
  // отстали на несколько шагов (вкладка спала) — догоняем быстрее
  off.value = Math.max(0, off.value - dt * Math.max(1, off.value) / period)
  const [tm, tx] = target.value, [m, x] = scale.value ?? [tm, tx], k = 1 - Math.exp(-dt / 260)
  const nm = m + (tm - m) * k, nx = x + (tx - x) * k
  const eps = (tx - tm || 1) * 1e-3
  scale.value = Math.abs(nm - tm) < eps && Math.abs(nx - tx) < eps ? [tm, tx] : [nm, nx]
  tail.value = tail.value.slice(-(Math.ceil(off.value) + 1))
  if (off.value > 0 || scale.value[0] !== tm || scale.value[1] !== tx) raf = requestAnimationFrame(frame)
  else stop()
}

let ro: ResizeObserver | undefined
let drawn = false
function firstDraw() {
  if (drawn || !pts.value.length) return
  drawn = true
  if (props.draw && wake.on) nextTick(() => drawSvg(svg.value, wake.at(props.draw![0]), props.draw![1]))
  if (!props.live) return
  const show = () => {
    liveOn.value = true
    if (wake.on) nextTick(() => anim(liveEl.value, [{ opacity: 0, transform: `${liveStyle.value.transform} scale(.3)` }, { opacity: 1, transform: liveStyle.value.transform }], { duration: 420, delay: props.liveIdx * 60 }))
  }
  if (wake.on) setTimeout(show, wake.at(2610))
  else show()
}

onMounted(() => {
  ro = new ResizeObserver(([e]) => { size.value = { w: e!.contentRect.width, h: e!.contentRect.height } })
  if (host.value) ro.observe(host.value)
  firstDraw()
})
onBeforeUnmount(() => {
  ro?.disconnect()
  cancelAnimationFrame(raf)
})

// Лентой едут только ряды без отметок (CPU узлов): у трафика и клиентов
// полоса и точки привязаны к часам, там новый ряд просто перерисовывается.
watch(() => props.data, (now, old) => {
  if (!drawn) return firstDraw()
  const shifted = old && old.length === now.length && old.length > 1
    && old.slice(1).join() === now.slice(0, -1).join()
  if (!shifted || props.band || props.dots.length || reduced() || document.hidden) return stop()
  const t = performance.now()
  if (lastShift && t - lastShift > 500 && t - lastShift < 10_000) period = period * 0.6 + (t - lastShift) * 0.4
  lastShift = t
  if (!scale.value) scale.value = range(old)
  tail.value = [...tail.value, old[0]!]
  off.value = Math.min(off.value + 1, 3)
  if (!raf) {
    last = t
    raf = requestAnimationFrame(frame)
  }
})
</script>

<template>
  <div ref="host" class="plot">
    <svg ref="svg" :viewBox="`0 0 ${w} ${h}`" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient :id="`${id}f`" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="var(--acc)" stop-opacity=".08" />
          <stop offset="1" stop-color="var(--acc)" stop-opacity="0" />
        </linearGradient>
        <linearGradient :id="`${id}b`" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="var(--acc)" stop-opacity=".07" />
          <stop offset="1" stop-color="var(--acc)" stop-opacity=".01" />
        </linearGradient>
        <!-- рамка ленты: по бокам режет, сверху и снизу оставляет место штриху -->
        <clipPath :id="`${id}c`">
          <polygon :points="`0,${-h} ${w},${-h} ${w},${2 * h} 0,${2 * h}`" />
        </clipPath>
      </defs>
      <rect v-if="bandRect" :x="bandRect.x" y="0" :width="bandRect.width" :height="h" :fill="`url(#${id}b)`" />
      <g v-if="d" :clip-path="`url(#${id}c)`">
        <path v-if="fill" :d="area" :fill="`url(#${id}f)`" />
        <path :d="d" fill="none" :style="{ stroke: col }" stroke-width="1.4" vector-effect="non-scaling-stroke" />
      </g>
    </svg>
    <span v-for="(p, i) in dotPos" :key="i" class="pt" :style="p" />
    <span v-if="liveOn" ref="liveEl" class="live" :class="{ bad: live === 'bad' }" :style="liveStyle" />
  </div>
</template>
