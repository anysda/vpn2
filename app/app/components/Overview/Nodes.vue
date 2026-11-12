<script setup lang="ts">
const props = defineProps<{ nodes: NodeMetric[], pings: Record<string, number | null>, refresh: () => Promise<unknown> }>()
const { toast, busy, act } = useAct()
const ask = useAsk()
const LAST_EXIT = 'Нельзя выключить последний включённый экзит'

// CPU за последние 13 опросов: начало из VictoriaMetrics, дальше каждый опрос дописывает точку.
const series = ref<Record<string, number[]>>({})
onMounted(async () => {
  const seed = await $fetch<Record<string, Array<number | null>>>('/api/ops/nodes-cpu').catch(() => ({}))
  for (const [tag, raw] of Object.entries(seed)) {
    let last = raw.find(v => v != null) ?? 0
    series.value[tag] = raw.map(v => (last = v ?? last))
  }
})
watch(() => props.nodes, (list) => {
  for (const n of list) {
    if (n.cpu != null && nodeHealth(n).kind !== 'offline') series.value[n.tag] = [...(series.value[n.tag] ?? []).slice(-12), n.cpu]
  }
})

function subtitle(n: NodeMetric) {
  if (n.tag === 'ru') return [n.host, uptimeShort(n.uptimeSec)].filter(Boolean).join(' · ')
  return [n.tag.toUpperCase(), props.pings[n.tag] != null ? `${props.pings[n.tag]} мс` : '', n.lanesWarp ? 'WARP' : ''].filter(Boolean).join(' · ')
}
const openTag = ref<string | null>(null)
const open = computed({ get: () => openTag.value !== null, set: (v) => { if (!v) openTag.value = null } })
const n = computed(() => props.nodes.find(x => x.tag === openTag.value) ?? null)
const mbps = (v: number | null | undefined) => v == null ? '—' : ru(v, v < 10 ? 1 : 0)
const LEVEL = { w: 'text-(--amber)', c: 'text-(--red-t)', '': '' }

const apply = (x: NodeMetric, disabled: boolean) => act('exit', async () => {
  await $fetch(`/api/ops/exits/${encodeURIComponent(x.tag)}`, { method: 'PATCH', body: { disabled } })
  await props.refresh()
}, { title: `${exitName(x.tag)} ${disabled ? 'выключен' : 'включён'}`, color: 'success' })

function toggle(x: NodeMetric) {
  if (x.disabled) return void apply(x, false)
  if (props.nodes.every(y => y.tag === x.tag || y.tag === 'ru' || y.disabled)) return toast.add({ title: LAST_EXIT, color: 'error' })
  if (!x.active && !x.activeDevices) return void apply(x, true)
  openTag.value = null
  ask.value = {
    title: `Выключить ${exitName(x.tag)}?`,
    text: `Через ${x.tag.toUpperCase()} сейчас идёт трафик. Выключить? Watchdog уведёт его на другие экзиты, текущие соединения оборвутся.`,
    ok: 'Выключить',
    danger: true,
    run: () => apply(x, true),
  }
}
</script>

<template>
  <section data-testid="ov-nodes" class="my-3 grid gap-3" :style="{ gridTemplateColumns: `repeat(auto-fit,minmax(min(100%,${nodes.length > 4 ? 200 : 240}px),1fr))` }">
    <button
      v-for="x in nodes" :key="x.tag" type="button" class="card text-left" :class="{ lit: nodeHealth(x).kind !== 'ok' && !x.disabled, 'opacity-50': x.disabled }"
      :aria-label="`${exitName(x.tag)}: подробности`" :data-tag="x.tag" @click="openTag = x.tag">
      <div class="font-medium">
        {{ exitName(x.tag) }}<small class="block text-xs font-normal text-(--t3)">{{ subtitle(x) }}</small>
      </div>
      <div class="mt-3 flex items-baseline gap-2">
        <span class="big">{{ nodeHealth(x).big }}</span><span class="text-sm text-(--t3)">{{ nodeHealth(x).bigLabel }}</span>
      </div>
      <LineChart :data="series[x.tag] ?? []" :h="44" :col="nodeHealth(x).kind === 'ok' ? 'var(--acc)' : 'var(--red-t)'" class="my-2" />
      <div class="flex items-center justify-between gap-2 text-xs text-(--t3)">
        <span class="tag" :class="x.disabled ? '' : nodeHealth(x).kind === 'ok' ? 'ok' : 'badt'">{{ x.disabled ? 'ВЫКЛ' : nodeHealth(x).tag }}</span>
        <span>{{ nodeHealth(x).meta }}</span>
      </div>
    </button>

    <PanelDialog v-model:open="open" :title="n ? exitName(n.tag) : ''" wide>
      <template v-if="n">
        <div class="flex items-center justify-between gap-3">
          <span class="sub2 mono">{{ n.host ?? n.instance }} · аптайм {{ uptimeShort(n.uptimeSec) }}</span>
          <div v-if="n.tag !== 'ru'" class="ha">
            <span class="tag">{{ n.disabled ? 'ВЫКЛ' : `${n.activeDevices ?? 0} устр.` }}</span>
            <PanelSwitch :model-value="!n.disabled" :label="`${exitName(n.tag)}: ${n.disabled ? 'включить' : 'выключить'}`" :disabled="busy.has('exit')" @update:model-value="toggle(n)" />
          </div>
        </div>
        <div class="kgrid mgrid">
          <div><span>CPU</span><b :class="LEVEL[level(n.cpu, 60, 80)]">{{ pct(n.cpu) }}</b></div>
          <div><span>iowait</span><b :class="LEVEL[level(n.iowait, 10, 30)]">{{ pct(n.iowait) }}</b></div>
          <div><span>steal</span><b :class="LEVEL[level(n.steal, 10, 30)]">{{ pct(n.steal) }}</b></div>
          <div><span>RAM</span><b :class="LEVEL[level(n.ram, 60, 80)]">{{ pct(n.ram) }}</b></div>
          <div title="Свободно на корневом разделе">
            <span>HDD свободно</span>
            <b :class="n.diskFreePct == null ? '' : n.diskFreePct < 10 ? LEVEL.c : n.diskFreePct < 20 ? LEVEL.w : ''">{{ n.diskFreeBytes == null ? '—' : gibLatin(n.diskFreeBytes) }}</b>
            <em v-if="n.diskFreeBytes != null && n.diskFreePct != null">{{ Math.round(n.diskFreePct) }}% из {{ gibTotalLatin(n.diskFreeBytes, n.diskFreePct) }}</em>
          </div>
          <div><span>Полос</span><b>{{ n.lanes ?? 0 }}</b><em>warp {{ n.lanesWarp ?? 0 }}</em></div>
          <div><span>↓ сейчас</span><b>{{ mbps(n.rxMbps) }}</b><em>Mbps</em></div>
          <div><span>↑ сейчас</span><b>{{ mbps(n.txMbps) }}</b><em>Mbps</em></div>
          <div><span>Сутки ↓</span><b>{{ gbDecRu(n.rxTodayBytes) }}</b><em>сутки ↑ {{ gbDecRu(n.txTodayBytes) }}</em></div>
        </div>
        <LineChart :data="series[n.tag] ?? []" :h="70" :col="nodeHealth(n).kind === 'ok' ? 'var(--acc)' : 'var(--red-t)'" />
      </template>
      <template #footer>
        <button type="button" class="pill" @click="open = false">
          Закрыть
        </button>
      </template>
    </PanelDialog>
  </section>
</template>
