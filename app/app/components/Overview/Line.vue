<script setup lang="ts">
import { anim, drawSvg, EIO, linePoints, smooth, useWake } from '~/utils/overview'

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

const id = useId()
const svg = ref<SVGSVGElement>()
const host = ref<HTMLElement>()
const wake = useWake()

const pts = computed(() => props.data.length > 1 ? linePoints(props.data, props.w, props.h, props.pad) : [])
const d = computed(() => pts.value.length ? smooth(pts.value) : '')
const bandRect = computed(() => {
  const p = pts.value, b = props.band
  if (!b || !p[b[0]] || !p[b[1]]) return null
  return { x: p[b[0]]![0], width: p[b[1]]![0] - p[b[0]]![0] }
})
const dotPos = computed(() => props.dots
  .filter(i => pts.value[i])
  .map(i => ({ left: `${pts.value[i]![0] / props.w * 100}%`, top: `${pts.value[i]![1] / props.h * 100}%` })))

// живая точка: px по размеру обёртки, движение — transition на transform
const size = ref({ w: 0, h: 0 })
const liveOn = ref(false)
const liveEl = ref<HTMLElement>()
const liveStyle = computed(() => {
  const last = pts.value.at(-1)
  if (!last) return {}
  return { transform: `translate(${size.value.w - 1}px,${last[1] / props.h * size.value.h}px)` }
})

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
onBeforeUnmount(() => ro?.disconnect())

// поток: ряд уехал на одно значение — пути въезжают справа налево
watch(() => props.data, (now, old) => {
  if (!drawn) return firstDraw()
  if (!old || old.length !== now.length || old.length < 2) return
  if (old.slice(1).join() !== now.slice(0, -1).join()) return
  const shift = props.w / (now.length - 1)
  nextTick(() => svg.value?.querySelectorAll('path').forEach(p =>
    anim(p, [{ transform: `translateX(${shift}px)` }, { transform: 'none' }], { duration: 900, easing: EIO, fill: 'none' })))
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
      </defs>
      <rect v-if="bandRect" :x="bandRect.x" y="0" :width="bandRect.width" :height="h" :fill="`url(#${id}b)`" />
      <template v-if="d">
        <path v-if="fill" :d="`${d} L${w},${h} L0,${h} Z`" :fill="`url(#${id}f)`" />
        <path :d="d" fill="none" :style="{ stroke: col }" stroke-width="1.4" vector-effect="non-scaling-stroke" />
      </template>
    </svg>
    <span v-for="(p, i) in dotPos" :key="i" class="pt" :style="p" />
    <span v-if="liveOn" ref="liveEl" class="live" :class="{ bad: live === 'bad' }" :style="liveStyle" />
  </div>
</template>
