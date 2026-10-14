<script setup lang="ts">
import type { NodeMetric } from '~/composables/useMonitoring'
import { anim } from '~/utils/clients-view'
import { ru, sweep, useWake } from '~/utils/overview'
import { exitName, nodeHealth } from '~/utils/overview-nodes'

// Выходы: доля каждого экзита в текущем трафике. Лидер — акцентом со
// свечением, остальные здоровые — оттенками серого одной строкой легенды,
// медленные и пропавшие — красным, каждый своей строкой.
const props = defineProps<{ nodes: NodeMetric[] }>()

const R = 60, W = 16, GAP = 3.2, C = 2 * Math.PI * R
const GREYS = ['var(--g1)', 'var(--g2)', 'var(--g3)', 'var(--g4)']

const exits = computed(() => props.nodes
  .filter(n => n.tag !== 'ru' && !n.disabled)
  .map(n => ({ n, v: Math.max(n.rxMbps ?? 0, n.txMbps ?? 0), bad: nodeHealth(n).kind !== 'ok' })))
const total = computed(() => exits.value.reduce((s, x) => s + x.v, 0))
const leader = computed(() => exits.value.filter(x => !x.bad).sort((a, b) => b.v - a.v)[0])

const segs = computed(() => {
  const t = total.value
  if (!t) return []
  let off = 0, g = 0
  const ordered = [...exits.value].sort((a, b) => Number(a.bad) - Number(b.bad) || Number(b === leader.value) - Number(a === leader.value) || b.v - a.v)
  return ordered.filter(x => x.v > 0).map((x) => {
    const len = Math.max(0, C * x.v / t - GAP)
    const seg = {
      key: x.n.tag,
      c: x.bad ? 'var(--red)' : x === leader.value ? 'var(--acc)' : GREYS[g++ % GREYS.length]!,
      glow: x === leader.value,
      dash: `${len} ${C - len}`,
      off: -off,
    }
    off += C * x.v / t
    return seg
  })
})

const pct = (v: number) => total.value ? `${Math.round(v / total.value * 100)}%` : '0%'
const rows = computed(() => {
  const out: Array<{ key: string, color: string, name: string, v: number }> = []
  const l = leader.value
  if (l) out.push({ key: l.n.tag, color: 'var(--acc)', name: exitName(l.n.tag), v: l.v })
  const rest = exits.value.filter(x => !x.bad && x !== l)
  if (rest.length) out.push({ key: 'rest', color: 'var(--g2)', name: rest.map(x => exitName(x.n.tag)).join(', '), v: rest.reduce((s, x) => s + x.v, 0) })
  for (const x of exits.value.filter(x => x.bad)) out.push({ key: x.n.tag, color: 'var(--red)', name: exitName(x.n.tag), v: x.v })
  return out
})

const svg = ref<SVGSVGElement>()
const legend = ref<HTMLElement>()
const wake = useWake()
let swept = false
watch(segs, (s) => {
  if (swept || !s.length) return
  swept = true
  if (!wake.on) return
  nextTick(() => {
    sweep(svg.value, wake.at(260), 1100)
    legend.value?.querySelectorAll(':scope>div').forEach((d, i) => anim(d, [{ opacity: 0, transform: 'translateX(-6px)' }, { opacity: 1, transform: 'none' }], { duration: 420, delay: wake.at(700 + (i % 3) * 70) }))
  })
}, { immediate: true })
</script>

<template>
  <div class="card donut ov-exits">
    <h3>Выходы</h3>
    <div class="ring">
      <svg ref="svg" viewBox="0 0 150 150" aria-hidden="true">
        <circle cx="75" cy="75" :r="R" fill="none" stroke="rgba(var(--fg),.04)" :stroke-width="W" />
        <circle
          v-for="s in segs" :key="s.key" cx="75" cy="75" :r="R" fill="none" :stroke-width="W"
          :stroke-dasharray="s.dash" :stroke-dashoffset="s.off" transform="rotate(-90 75 75)" stroke-linecap="butt"
          :style="{ stroke: s.c, filter: s.glow ? 'drop-shadow(0 0 6px rgba(var(--glow),calc(.45*var(--gk))))' : undefined }"
        />
      </svg>
      <div class="ct">
        <OverviewNum tag="b" :value="nodes.length ? ru(total) : '—'" :dur="1100" :delay="260" /><span>Мбит/с сейчас</span>
      </div>
    </div>
    <div ref="legend" class="leg">
      <div v-for="r in rows" :key="r.key">
        <span class="dot" :style="{ background: r.color }" /><span class="nm">{{ r.name }}</span><em>{{ ru(r.v, 1) }}</em><i>{{ pct(r.v) }}</i>
      </div>
    </div>
  </div>
</template>
