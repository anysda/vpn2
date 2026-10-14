<script setup lang="ts">
import { peakBand, ru } from '~/utils/overview'

// Клиенты онлайн и офлайн: сейчас и по часам за сутки. Полоса и точки —
// на четырёх часах подряд, где ряд выше всего.
interface ClientsOnline { total: number, online: number, hourly: number[] }

const { data, refresh } = useFetch<ClientsOnline>('/api/ops/clients-online', { server: false })
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => void refresh(), 60_000) })
onBeforeUnmount(() => clearInterval(timer))

const cards = computed(() => {
  const d = data.value
  if (!d) {
    return [
      { key: 'on', title: 'Клиенты онлайн', value: '—', share: '—', data: [], band: null, live: true },
      { key: 'off', title: 'Клиенты офлайн', value: '—', share: '—', data: [], band: null, live: false },
    ]
  }
  const off = d.total - d.online
  const share = (v: number) => d.total ? `${Math.round(v / d.total * 100)}%` : '0%'
  const on = d.hourly, offH = d.hourly.map(v => Math.max(0, d.total - v))
  const bOn = peakBand(on), bOff = peakBand(offH)
  return [
    { key: 'on', title: 'Клиенты онлайн', value: ru(d.online), share: share(d.online), data: on, band: bOn, live: true },
    { key: 'off', title: 'Клиенты офлайн', value: ru(off), share: share(off), data: offH, band: bOff, live: false },
  ]
})
</script>

<template>
  <div class="stack ov-clients">
    <div v-for="(c, i) in cards" :key="c.key" class="card mini">
      <h3>{{ c.title }}</h3>
      <div class="nums"><OverviewNum class="big" :value="c.value" :dur="900" :delay="1000 + i * 120" /><span class="tag">{{ c.share }}</span></div>
      <OverviewLine
        :data="c.data" :w="200" :h="80" :fill="false" :band="c.band" :dots="c.band ?? []"
        :live="c.live" :live-idx="1" :draw="[1000 + i * 120, 1000]"
      />
    </div>
  </div>
</template>
