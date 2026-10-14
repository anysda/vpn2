<script setup lang="ts">
import { linePoints, peakBand, ru } from '~/utils/overview'

// Трафик за сутки (по часам) или неделю (по суткам МСК): сумма экзитов,
// рядом — сравнение с тем же окном периодом раньше. Линейка ведёт по точкам.
type Period = 'day' | 'week'
interface Traffic { period: Period, step: number, starts: number[], total: number[], sum: number, prev: number | null }

const props = defineProps<{ period: Period }>()
const { data, refresh } = useFetch<Traffic>('/api/ops/traffic', { query: computed(() => ({ period: props.period })), server: false })
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => void refresh(), 60_000) })
onBeforeUnmount(() => clearInterval(timer))

const W = 400, H = 140, PAD = 14
const gb = computed(() => (data.value?.total ?? []).map(b => b / 1e9))
const band = computed(() => peakBand(gb.value, 5))
const big = computed(() => data.value ? ru(data.value.sum / 1e9, data.value.sum < 1e10 ? 1 : 0) : '—')
const delta = computed(() => {
  const d = data.value
  if (!d?.prev) return null
  const pct = Math.round((d.sum - d.prev) / d.prev * 100)
  return `${pct > 0 ? '+' : pct < 0 ? '−' : ''}${Math.abs(pct)}% ${d.period === 'day' ? 'ко вчера' : 'к прошлой неделе'}`
})

const D = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб']
const MG = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
const hh = (h: number) => `${String(h).padStart(2, '0')}:00`
function tipText(i: number) {
  const d = data.value!
  // подписи по Москве: сдвигаем на +3 ч и читаем как UTC
  const t = new Date((d.starts[i]! + 3 * 3600) * 1000), h = t.getUTCHours()
  return {
    v: `${ru(gb.value[i]!, 1)} ГБ`,
    unit: d.period === 'day' ? 'час' : 'сутки',
    sub: d.period === 'day' ? `${hh(h)}–${hh((h + 1) % 24)}` : `${D[t.getUTCDay()]}, ${t.getUTCDate()} ${MG[t.getUTCMonth()]}`,
  }
}

// линейка: курсор ведёт по точкам, подсказка плавно догоняет, у левого края — справа
const card = ref<HTMLElement>()
const rul = ref<HTMLElement>()
const tip = ref<HTMLElement>()
const hv = ref(false)
const now = ref(true)
const flip = ref(false)
const tipInfo = ref({ v: '', unit: '', sub: '' })
function svgRect() { return card.value?.querySelector('svg')?.getBoundingClientRect() }
function idx(e: PointerEvent) {
  const r = svgRect(), n = gb.value.length
  if (!r || n < 2) return -1
  return Math.max(0, Math.min(n - 1, Math.round((e.clientX - r.left) / r.width * (n - 1))))
}
function place(i: number, jump: boolean) {
  const r = svgRect(), c = card.value?.getBoundingClientRect()
  const p = linePoints(gb.value, W, H, PAD)[i]
  if (!r || !c || !p || !rul.value || !tip.value) return
  now.value = jump
  const x = r.left - c.left + p[0] / W * r.width, y = r.top - c.top + p[1] / H * r.height
  rul.value.style.top = `${r.top - c.top - card.value!.clientTop}px`
  rul.value.style.height = `${r.height}px`
  rul.value.style.transform = `translateX(${x}px)`
  tip.value.style.transform = `translate(${x}px,${y}px)`
  flip.value = i < gb.value.length * 0.3
  tipInfo.value = tipText(i)
}
function enter(e: PointerEvent) {
  const i = idx(e)
  if (i < 0) return
  const first = !hv.value
  hv.value = true
  place(i, first)
}
function move(e: PointerEvent) {
  const i = idx(e)
  if (i >= 0 && hv.value) place(i, false)
}
</script>

<template>
  <div ref="card" class="card wave ov-wave" :class="{ hv }" @pointerenter="enter" @pointermove="move" @pointerleave="hv = false">
    <h3>Трафик за {{ period === 'day' ? 'сутки' : 'неделю' }}</h3>
    <div class="top">
      <span class="big"><OverviewNum :value="big" :dur="1300" :delay="120" /><small>ГБ</small></span>
      <span><span v-if="delta" class="tag">{{ delta }} <span class="q" :title="period === 'day' ? 'Сравнение с тем же временем вчера' : 'Сравнение с тем же временем неделю назад'">?</span></span></span>
    </div>
    <OverviewLine
      :data="gb" :w="W" :h="H" :pad="PAD" :band="band" :dots="band ?? []"
      live :draw="[120, 1300]"
    />
    <span ref="rul" class="rul" :class="{ now }" />
    <span ref="tip" class="qtip wv" :class="{ now, r: flip }"><i /><div><b>{{ tipInfo.v }}</b> за {{ tipInfo.unit }}<small>{{ tipInfo.sub }}</small></div></span>
  </div>
</template>
