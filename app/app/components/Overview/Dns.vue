<script setup lang="ts">
import { anim } from '~/utils/clients-view'
import { ru, useWake } from '~/utils/overview'

// DNS сегодня: кольцо из 11 сегментов, акцентный первый — как в макете.
// Данные — та же сводка AdGuard, что в разделе DNS.
interface AdguardSummary { available: boolean, totalToday: number, blockedToday: number, blockRatePct: number, avgMs: number }

const { data, refresh } = useFetch<AdguardSummary>('/api/ops/adguard', { server: false })
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => void refresh(), 30_000) })
onBeforeUnmount(() => clearInterval(timer))

const N = 11, R = 58, W = 22, C = 2 * Math.PI * R, SEG = C / N, GAP = 5
const ok = computed(() => Boolean(data.value?.available))

const svg = ref<SVGSVGElement>()
const legend = ref<HTMLElement>()
const wake = useWake()
let shown = false
watch(data, (d) => {
  if (shown || !d) return
  shown = true
  if (!wake.on) return
  nextTick(() => {
    svg.value?.querySelectorAll('circle').forEach((c, i) => anim(c, [{ opacity: 0 }, { opacity: 1 }], { duration: 260, delay: wake.at(380 + i * 70) }))
    legend.value?.querySelectorAll(':scope>div').forEach((d, i) => anim(d, [{ opacity: 0, transform: 'translateX(-6px)' }, { opacity: 1, transform: 'none' }], { duration: 420, delay: wake.at(700 + (i % 3) * 70) }))
  })
}, { immediate: true })
</script>

<template>
  <div class="card donut ov-dns">
    <h3>DNS сегодня</h3>
    <div class="ring">
      <svg ref="svg" viewBox="0 0 150 150" aria-hidden="true">
        <circle
          v-for="i in N" :key="i" cx="75" cy="75" :r="R" fill="none" :stroke-width="W"
          :stroke-dasharray="`${SEG - GAP} ${C - SEG + GAP}`" :stroke-dashoffset="-(i - 1) * SEG" transform="rotate(-90 75 75)"
          :style="i === 1 ? { stroke: 'var(--acc)', filter: 'drop-shadow(0 0 6px rgba(var(--glow),calc(.5*var(--gk))))' } : { stroke: 'var(--g5)' }"
        />
      </svg>
      <div class="ct">
        <OverviewNum tag="b" :value="ok ? `${ru(data!.blockRatePct, 1)}%` : '—'" :dur="900" :delay="380" /><span>заблокировано</span>
      </div>
    </div>
    <div ref="legend" class="leg">
      <div><span class="dot" style="background:var(--acc)" />Заблокировано<em>{{ ok ? ru(data!.blockedToday) : '—' }}</em><i /></div>
      <div><span class="dot" style="background:var(--g4);box-shadow:inset 0 0 0 1px var(--g2)" />Запросов<em>{{ ok ? ru(data!.totalToday) : '—' }}</em><i /></div>
      <div><span class="dot" style="background:transparent;box-shadow:inset 0 0 0 1px var(--g2)" />Ответ в среднем<em>{{ ok ? `${ru(data!.avgMs, 1)} мс` : '—' }}</em><i /></div>
    </div>
  </div>
</template>
