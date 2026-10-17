<script setup lang="ts">
import { anim, sizeRu } from '~/utils/clients-view'
import { useWake } from '~/utils/overview'

// Топ клиентов по трафику за сутки или неделю (переключатель в шапке Обзора).
// Полоса — доля от лидера, клик открывает клиента.
interface TopClient { id: number, name: string, rx: number, tx: number, total: number }
interface Top { clients: TopClient[] }

const props = defineProps<{ period: 'day' | 'week' }>()
const query = computed(() => ({ period: props.period }))
const { data } = useFetch<Top>('/api/clients/top', { query, server: false })

// на телефоне — первые три
const mobile = useMobile()
const rows = computed(() => {
  const all = data.value?.clients.filter(c => c.total > 0) ?? []
  const list = mobile.value ? all.slice(0, 3) : all
  const max = list[0]?.total || 1
  return list.map(c => ({ ...c, w: `${Math.max(2, c.total / max * 100)}%` }))
})

const list = ref<HTMLElement>()
const wake = useWake()
let shown = false
watch(rows, (r) => {
  if (shown || !r.length) return
  shown = true
  if (!wake.on) return
  nextTick(() => list.value?.querySelectorAll(':scope>button').forEach((b, i) =>
    anim(b, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: 380, delay: wake.at(900 + Math.min(i, 6) * 60) })))
}, { immediate: true })
</script>

<template>
  <div class="card top ov-top">
    <div class="fhead">
      <h3>Топ клиентов</h3>
    </div>
    <div v-if="rows.length" ref="list" class="tl">
      <button v-for="(c, i) in rows" :key="c.id" type="button" class="tr" @click="navigateTo({ path: '/clients', query: { id: c.id } })">
        <span class="n">{{ i + 1 }}</span>
        <span class="nm">{{ c.name }}</span>
        <b>{{ sizeRu(c.total) }}</b>
        <span class="bar"><i :class="{ lead: i === 0 }" :style="{ width: c.w }" /></span>
        <span class="io">↓ {{ sizeRu(c.rx) }} · ↑ {{ sizeRu(c.tx) }}</span>
      </button>
    </div>
    <div v-else class="empty">{{ data ? 'Трафика пока нет' : '' }}</div>
  </div>
</template>

<style scoped>
.top{display:flex;flex-direction:column;padding:16px 18px 10px;height:272px;min-width:0}
.top .fhead h3{line-height:28px}
.tl{flex:1;min-height:0;overflow-y:auto;margin:0 -8px;scrollbar-width:thin}
.tr{display:grid;grid-template-columns:18px minmax(0,1fr) auto;column-gap:8px;row-gap:5px;align-items:center;width:100%;padding:8px;border:0;border-radius:10px;background:none;color:inherit;font:inherit;text-align:left;cursor:pointer;transition:background-color .16s}
@media (hover:hover){.tr:hover{background:rgba(var(--fg),.04)}}
.n{font-size:11px;color:var(--t3);font-variant-numeric:tabular-nums}
.nm{font-size:13px;color:var(--t1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tr b{font-size:13px;font-weight:500;color:var(--t1);font-variant-numeric:tabular-nums}
.bar{grid-column:2;height:4px;border-radius:2px;background:rgba(var(--fg),.05)}
.bar i{display:block;height:100%;border-radius:2px;background:rgba(var(--fg),.28)}
.bar i.lead{background:var(--acc);box-shadow:0 0 8px rgba(var(--glow),calc(.4*var(--gk)))}
.io{font-size:11px;color:var(--t3);white-space:nowrap;font-variant-numeric:tabular-nums}
.empty{flex:1;display:grid;place-items:center;font-size:13px;color:var(--t3)}
/* телефон: три строки без своей прокрутки, нажатие подсвечивает строку */
@media (max-width:720px){
  .top{height:auto;padding:16px 10px 10px}
  .top .fhead{margin:0 8px 6px}
  .tl{flex:none;overflow:visible;margin:0}
  .tr{touch-action:manipulation;-webkit-user-select:none;user-select:none}
  .tr:active{background:rgba(var(--fg),.05)}
  .empty{min-height:96px}
}
</style>
