<script setup lang="ts">
import type { EventLevel, PanelEvent } from '#shared/events'

const { data, refresh } = useFetch<PanelEvent[]>('/api/events', { query: { limit: 30 }, default: () => [] })

// Журнал пишется сторожем раз в 30 с — чаще спрашивать незачем.
let timer: ReturnType<typeof setInterval> | null = null
onMounted(() => { timer = setInterval(() => void refresh(), 30_000) })
onUnmounted(() => { if (timer) clearInterval(timer) })
useVisibleRefresh(refresh)

// края ленты гаснут только там, где за ними что-то есть
const list = ref<HTMLElement>()
const edge = reactive({ top: false, end: false })
function onScroll() {
  const u = list.value
  if (!u) return
  edge.top = u.scrollTop > 1
  edge.end = u.scrollTop + u.clientHeight >= u.scrollHeight - 1
}
onMounted(onScroll)

// «Сегодня» сервер и первая отрисовка браузера считают от одного момента:
// время рендера едет в payload, иначе при других часах браузера гидратация
// расходится. После монтирования и на каждом опросе — часы браузера.
const now = useState('events-feed-now', () => Date.now())
onMounted(() => { now.value = Date.now() })
watch(data, () => {
  now.value = Date.now()
  void nextTick(onScroll)
})

const COLOR: Record<EventLevel, string> = { alarm: 'var(--red)', warn: 'var(--amber)', info: 'var(--g2)', ok: 'var(--green)' }

const MSK = 'Europe/Moscow'
const dayOf = (ms: number) => new Date(ms).toLocaleDateString('ru-RU', { timeZone: MSK })
/** Сегодня — время, раньше — дата. */
function when(ms: number): string {
  if (dayOf(ms) === dayOf(now.value)) {
    return new Date(ms).toLocaleTimeString('ru-RU', { timeZone: MSK, hour: '2-digit', minute: '2-digit' })
  }
  return new Date(ms).toLocaleDateString('ru-RU', { timeZone: MSK, day: '2-digit', month: '2-digit' })
}
</script>

<template>
  <div class="card ev">
    <h3>События</h3>
    <ul v-if="data.length" ref="list" :class="{ top: edge.top, end: edge.end }" @scroll.passive="onScroll">
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
/* Первая карточка Обзора: высота ряда задана соседями, лента едет внутри. */
.ev{display:flex;flex-direction:column;min-height:0}
.ev ul{list-style:none;flex:1;min-height:0;margin:12px -8px 0 0;padding:2px 8px 14px 0;overflow-y:auto;overscroll-behavior:contain;
  scrollbar-width:thin;scrollbar-color:rgba(var(--fg),.14) transparent;
  --ft:0px;--fb:22px;mask-image:linear-gradient(180deg,transparent,#000 var(--ft),#000 calc(100% - var(--fb)),transparent)}
.ev ul.top{--ft:18px}
.ev ul.end{--fb:0px}
.ev ul::-webkit-scrollbar{width:8px}
.ev ul::-webkit-scrollbar-thumb{background:rgba(var(--fg),.12);border-radius:8px;border:2px solid transparent;background-clip:padding-box}
.ev li{display:grid;grid-template-columns:40px 8px 1fr;column-gap:10px;position:relative;padding-bottom:14px}
.ev li:last-child{padding-bottom:0}
.ev time{grid-row:1/3;font:400 12px/18px ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-variant-numeric:tabular-nums;color:var(--t3);white-space:nowrap}
.ev li>.dot{grid-row:1;margin-top:5px;width:8px;height:8px;justify-self:center}
.ev li:not(:last-child)::after{content:"";position:absolute;left:53.5px;top:19px;bottom:1px;width:1px;background:rgba(var(--fg),.09)}
.ev b{display:block;font-weight:500;font-size:13px;line-height:18px;color:var(--t1);overflow-wrap:anywhere}
.ev small{display:block;margin-top:2px;font-size:11.5px;line-height:16px;color:var(--t3);overflow-wrap:anywhere}
.ev li>div{min-width:0}
.ev .empty{margin-top:12px;font-size:12.5px;color:var(--t3)}
/* живое: пульс у «снова в сети» и у тревоги, период 2,4 с */
.dot.pulse{position:relative}
.dot.pulse::after{content:"";position:absolute;inset:0;border-radius:50%;background:inherit;animation:halo 2.4s var(--ease) infinite;pointer-events:none}
@keyframes halo{0%{transform:scale(1);opacity:.6}75%,100%{transform:scale(3);opacity:0}}
@media (prefers-reduced-motion:reduce){
  .dot.pulse::after{animation:none}
}
</style>
