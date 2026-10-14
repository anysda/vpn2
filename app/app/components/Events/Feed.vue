<script setup lang="ts">
import type { EventLevel, PanelEvent } from '#shared/events'

const { data, refresh } = useFetch<PanelEvent[]>('/api/events', { query: { limit: 4 }, default: () => [] })

// Журнал пишется сторожем раз в 30 с — чаще спрашивать незачем.
let timer: ReturnType<typeof setInterval> | null = null
onMounted(() => { timer = setInterval(() => void refresh(), 30_000) })
onUnmounted(() => { if (timer) clearInterval(timer) })
useVisibleRefresh(refresh)

const COLOR: Record<EventLevel, string> = { alarm: 'var(--red)', warn: 'var(--amber)', info: 'var(--g2)', ok: 'var(--green)' }

const MSK = 'Europe/Moscow'
const dayOf = (ms: number) => new Date(ms).toLocaleDateString('ru-RU', { timeZone: MSK })
/** Сегодня — время, раньше — дата. */
function when(ms: number): string {
  if (dayOf(ms) === dayOf(Date.now())) {
    return new Date(ms).toLocaleTimeString('ru-RU', { timeZone: MSK, hour: '2-digit', minute: '2-digit' })
  }
  return new Date(ms).toLocaleDateString('ru-RU', { timeZone: MSK, day: '2-digit', month: '2-digit' })
}
</script>

<template>
  <div class="card ev">
    <h3>События</h3>
    <ul v-if="data.length">
      <li v-for="e in data" :key="e.id">
        <time :datetime="new Date(e.at).toISOString()">{{ when(e.at) }}</time>
        <span class="dot" :class="{ pulse: e.level === 'ok' || e.level === 'alarm' }" :style="{ background: COLOR[e.level] }" />
        <div><b>{{ e.title }}</b><small v-if="e.detail">{{ e.detail }}</small></div>
      </li>
    </ul>
    <p v-else class="empty">
      Событий пока нет
    </p>
  </div>
</template>

<style scoped>
.ev{display:flex;flex-direction:column;padding:20px 22px;min-height:300px}
.ev h3{color:var(--t1);font-size:15px;line-height:28px}
.ev ul{list-style:none;margin-top:14px}
.ev li{display:grid;grid-template-columns:56px 14px 1fr;column-gap:12px;position:relative;padding-bottom:16px}
.ev li:last-child{padding-bottom:0}
.ev time{grid-row:1/3;font:400 13px/20px ui-monospace,"SF Mono",Menlo,Consolas,monospace;color:var(--t3)}
.ev li>.dot{grid-row:1;margin-top:6px;width:8px;height:8px;justify-self:center}
.ev li:not(:last-child)::after{content:"";position:absolute;left:74px;top:22px;bottom:2px;width:1px;background:rgba(var(--fg),.09)}
.ev b{display:block;font-weight:500;font-size:14.5px;line-height:20px;color:var(--t1)}
.ev small{display:block;margin-top:3px;font-size:12.5px;color:var(--t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ev li>div{min-width:0}
.ev .empty{margin-top:14px;font-size:12.5px;color:var(--t3)}
/* живое: пульс у «снова в сети» и у тревоги, период 2,4 с */
.dot.pulse{position:relative}
.dot.pulse::after{content:"";position:absolute;inset:0;border-radius:50%;background:inherit;animation:halo 2.4s var(--ease) infinite;pointer-events:none}
@keyframes halo{0%{transform:scale(1);opacity:.6}75%,100%{transform:scale(3);opacity:0}}
@media (prefers-reduced-motion:reduce){
  .dot.pulse::after{animation:none}
}
</style>
