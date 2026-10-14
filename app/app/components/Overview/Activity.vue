<script setup lang="ts">
import { pluralRu } from '#shared/plural'
import { anim, ru, useWake } from '~/utils/overview'

// Активность: недели столбцами, дни строками, как в GitHub. «Онлайн» —
// клиентов в сети за сутки, «Трафик» — ГБ всех клиентов за сутки (МСК).
interface Day { day: number, online: number, bytes: number }
const { data, refresh } = useFetch<{ today: number, days: Day[] }>('/api/ops/activity', { server: false })
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => void refresh(), 300_000) })
onBeforeUnmount(() => clearInterval(timer))

type Mode = 'on' | 'tr'
const mode = ref<Mode>('on')
const D = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
const MO = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
const MG = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
// номер суток МСК → дата, читаемая через getUTC*
const date = (day: number) => new Date(day * 864e5)
const wd = (day: number) => (date(day).getUTCDay() + 6) % 7

const byDay = computed(() => new Map((data.value?.days ?? []).map(d => [d.day, d])))
const val = (day: number) => {
  const d = byDay.value.get(day)
  if (!d) return 0
  return mode.value === 'on' ? d.online : Math.round(d.bytes / 1e8) / 10
}

// размер клетки — от ширины сетки, как в макете
const grid = ref<HTMLElement>()
const width = ref(0)
let ro: ResizeObserver | undefined
onMounted(() => {
  ro = new ResizeObserver(([e]) => { width.value = Math.round(e!.contentRect.width) })
  if (grid.value) ro.observe(grid.value)
})
onBeforeUnmount(() => ro?.disconnect())

const geo = computed(() => {
  const W = width.value
  if (W < 120 || !data.value) return null
  const N = Math.floor((W - 26) / 22), c = (W - 26) / N - 4
  const today = data.value.today, start = today - wd(today) - 7 * (N - 1)
  const cells: Array<{ w: number, d: number, day: number, v: number }> = []
  for (let w = 0; w < N; w++) for (let d = 0; d < 7; d++) {
    const day = start + w * 7 + d
    if (day <= today) cells.push({ w, d, day, v: val(day) })
  }
  const months: Array<{ w: number, m: string }> = []
  let lastM = -1
  for (let w = 0; w < N; w++) {
    const t = date(start + w * 7)
    if (t.getUTCMonth() !== lastM && w < N - 2) {
      if (lastM >= 0 || t.getUTCDate() <= 7) months.push({ w, m: MO[t.getUTCMonth()]! })
      lastM = t.getUTCMonth()
    }
  }
  const mx = Math.max(0, ...cells.map(x => x.v))
  const pk = mx > 0 ? cells.find(x => x.v === mx)!.day : null
  return { N, c, cells, months, mx, pk }
})
const level = (v: number, mx: number) => {
  const t = mx ? v / mx : 0
  return t < 0.2 ? 0.05 : t < 0.4 ? 0.14 : t < 0.6 ? 0.28 : t < 0.8 ? 0.5 : 0.9
}

// выбранный день: по умолчанию пик, наведение переносит выбор
const sel = ref<number | null>(null)
const selDay = computed(() => {
  const g = geo.value
  if (!g) return null
  return sel.value != null && g.cells.some(x => x.day === sel.value) ? sel.value : g.pk ?? data.value!.today
})
const read = computed(() => {
  const day = selDay.value
  if (day == null) return null
  const t = date(day), v = val(day)
  return {
    when: `${D[wd(day)]}, ${t.getUTCDate()} ${MG[t.getUTCMonth()]}`,
    v: mode.value === 'on' ? String(v) : ru(v, 1),
    unit: mode.value === 'on' ? `${pluralRu(v, ['клиент', 'клиента', 'клиентов'])} в сети` : 'ГБ',
  }
})

// сетка накатывает неделями, слева направо: в сцене пробуждения и при смене режима
const wake = useWake()
function ripple(delay = 0) {
  grid.value?.querySelectorAll<HTMLElement>(':scope>i').forEach((c) => {
    const w = Number.parseInt(c.style.gridColumn) || 0
    anim(c, [{ opacity: 0, transform: 'scale(.55)' }, { opacity: 1, transform: 'none' }], { duration: 380, delay: delay + Math.min(w * 16, 620) })
  })
}
let rippled = false
watch(geo, (g) => {
  if (rippled || !g) return
  rippled = true
  if (wake.on) nextTick(() => ripple(wake.at(900)))
}, { immediate: true })

// пилюля сегмента скользит к выбранной кнопке
const seg = ref<HTMLElement>()
const glide = ref<HTMLElement>()
const glided = ref(false)
function moveGlide(jump: boolean) {
  const box = seg.value, g = glide.value, on = box?.querySelector<HTMLElement>(':scope>button.on')
  if (!box || !g || !on || !box.offsetWidth) return
  const r = on.getBoundingClientRect(), q = box.getBoundingClientRect()
  g.classList.toggle('now', jump || !glided.value)
  g.style.height = `${r.height}px`
  g.style.width = `${r.width}px`
  g.style.transform = `translate(${r.left - q.left - box.clientLeft}px,${r.top - q.top - box.clientTop}px)`
  if (jump || !glided.value) requestAnimationFrame(() => g.classList.remove('now'))
  glided.value = true
}
const onResize = () => moveGlide(true)
onMounted(() => {
  requestAnimationFrame(() => moveGlide(true))
  void document.fonts?.ready.then(() => moveGlide(true))
  addEventListener('resize', onResize)
})
onBeforeUnmount(() => removeEventListener('resize', onResize))

function pick(m: Mode) {
  if (m === mode.value) return
  mode.value = m
  nextTick(() => {
    requestAnimationFrame(() => moveGlide(false))
    ripple()
  })
}
</script>

<template>
  <div class="card act ov-activity">
    <div class="fhead">
      <h3>Активность</h3>
      <div ref="seg" class="seg sm" :class="{ gl: glided }" role="group" aria-label="Что показывать">
        <button type="button" :class="{ on: mode === 'on' }" :aria-pressed="mode === 'on'" @click="pick('on')">Онлайн</button>
        <button type="button" :class="{ on: mode === 'tr' }" :aria-pressed="mode === 'tr'" @click="pick('tr')">Трафик</button>
        <span ref="glide" class="glide now" />
      </div>
    </div>
    <div
      ref="grid" class="agrid"
      :style="geo ? { gridTemplateColumns: `22px repeat(${geo.N},${geo.c}px)`, gridTemplateRows: `14px repeat(7,${geo.c}px)` } : undefined"
    >
      <template v-if="geo">
        <span v-for="d in [0, 2, 4]" :key="`d${d}`" class="hd" :style="{ gridRow: d + 2, gridColumn: 1 }">{{ D[d] }}</span>
        <span v-for="m in geo.months" :key="`m${m.w}`" class="hh" :style="{ gridRow: 1, gridColumn: `${m.w + 2}/span 4` }">{{ m.m }}</span>
        <i
          v-for="x in geo.cells" :key="x.day" :data-day="x.day"
          :class="{ pk: x.day === geo.pk, sel: x.day === selDay }"
          :style="{ '--a': level(x.v, geo.mx), gridRow: x.d + 2, gridColumn: x.w + 2 }"
          @mouseenter="sel = x.day"
        />
      </template>
    </div>
    <div class="afoot">
      <div class="aleg"><span>меньше</span><i style="--a:.05" /><i style="--a:.14" /><i style="--a:.28" /><i style="--a:.5" /><i style="--a:.9" /><span>больше</span></div>
      <span v-if="read" class="aread">{{ read.when }} · <b>{{ read.v }}</b> {{ read.unit }}</span>
    </div>
  </div>
</template>
