<script setup lang="ts">
import { plural } from '#shared/plural'

// Недели столбцами, дни строками. «Онлайн» - клиентов в сети за сутки, «Трафик» - ГБ всех клиентов за сутки МСК.
interface Day { day: number, online: number, bytes: number }
const { data, refresh } = useFetch<{ today: number, days: Day[] }>('/api/ops/activity', { server: false })
usePoll(refresh, 300_000)

const MODES = [{ value: 'on', label: 'Онлайн' }, { value: 'tr', label: 'Трафик' }] as const
const MO = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
const SHADE = [0.05, 0.14, 0.28, 0.5, 0.9]
const mode = ref<'on' | 'tr'>('on')
const mobile = useMobile()
const date = (day: number) => new Date(day * 864e5)
const raw = (d?: Day) => d ? (mode.value === 'on' ? d.online : d.bytes) : 0

// Ступени по квартилям ненулевых дней; различимых значений не больше четырёх - каждому своя, наибольшее темнее всех.
function scale(values: number[]) {
  const s = values.filter(v => v > 0).sort((a, b) => a - b), u = [...new Set(s)]
  if (u.length <= 4) return (v: number) => v > 0 ? (u.length === 1 ? 4 : 1 + Math.round(u.indexOf(v) * 3 / (u.length - 1))) : 0
  const th = [1, 2, 3].map(k => s[Math.floor(k * s.length / 4)]!)
  return (v: number) => v > 0 ? 1 + th.filter(t => v >= t).length : 0
}

const grid = computed(() => {
  if (!data.value) return null
  const weeks = mobile.value ? 14 : 26, today = data.value.today
  const byDay = new Map(data.value.days.map(d => [d.day, d]))
  const start = today - (date(today).getUTCDay() + 6) % 7 - 7 * (weeks - 1)
  const days = Array.from({ length: weeks * 7 }, (_, i) => start + i).filter(day => day <= today)
  const step = scale(days.map(d => raw(byDay.get(d))))
  const months: { w: number, m: string }[] = []
  for (let w = 0, last = -1; w < weeks - 2; w++) {
    const t = date(start + w * 7)
    if (t.getUTCMonth() === last) continue
    if (last >= 0 || t.getUTCDate() <= 7) months.push({ w, m: MO[t.getUTCMonth()]! })
    last = t.getUTCMonth()
  }
  const peak = days.reduce((p, d) => raw(byDay.get(d)) > raw(byDay.get(p)) ? d : p, today)
  return { weeks, byDay, peak, months, cells: days.map((day, i) => ({ day, w: Math.floor(i / 7), d: i % 7, a: SHADE[step(raw(byDay.get(day)))]! })) }
})
const sel = ref<number | null>(null)
const read = computed(() => {
  const g = grid.value
  if (!g) return null
  const day = sel.value ?? g.peak, d = g.byDay.get(day)
  if (mode.value === 'on') return { when: mskDayNumLabel(day), v: d?.online ?? 0, unit: `${plural(d?.online ?? 0, 'client')} в сети` }
  return { when: mskDayNumLabel(day), v: ru((d?.bytes ?? 0) / 1e9, 1), unit: 'ГБ' }
})
</script>

<template>
  <div class="card flex flex-col">
    <div class="fhead">
      <h3>Активность</h3>
      <Seg v-model="mode" :options="MODES" small />
    </div>
    <div v-if="grid" class="mt-3 grid items-center gap-1 text-[11px] text-(--t3)" :style="{ gridTemplateColumns: `22px repeat(${grid.weeks},minmax(0,1fr))` }">
      <span v-for="m in grid.months" :key="`m${m.w}`" class="h-3.5 whitespace-nowrap" :style="{ gridRow: 1, gridColumn: `${m.w + 2}/span 4` }">{{ m.m }}</span>
      <span v-for="d in [0, 2, 4]" :key="`d${d}`" :style="{ gridRow: d + 2, gridColumn: 1 }">{{ WD_MON[d] }}</span>
      <i
        v-for="c in grid.cells" :key="c.day" class="aspect-square cursor-pointer rounded" :class="{ 'ring-2 ring-(--acc)': c.day === (sel ?? grid.peak) }"
        :style="{ background: `rgba(var(--fg),${c.a})`, gridRow: c.d + 2, gridColumn: c.w + 2 }" @mouseenter="sel = c.day" @click="sel = c.day" />
    </div>
    <div class="mt-auto flex flex-wrap items-center justify-between gap-2 pt-2.5 text-xs text-(--t3)">
      <span class="flex items-center gap-1">меньше<i v-for="a in SHADE" :key="a" class="size-2.5 rounded-[3px]" :style="{ background: `rgba(var(--fg),${a})` }" />больше</span>
      <span v-if="read">{{ read.when }} · <b class="font-medium text-(--t1)">{{ read.v }}</b> {{ read.unit }}</span>
    </div>
  </div>
</template>
