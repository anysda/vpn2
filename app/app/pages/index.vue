<script setup lang="ts">
import type { EventLevel, PanelEvent } from '#shared/events'
import { parseOutbound } from '#shared/outbounds'

useHead({ title: 'Обзор — anysda-vpn2' })
definePageMeta({ title: 'Обзор' })
const mobile = useMobile()
const period = useState<'day' | 'week'>('ov-period', () => 'day')
onUnmounted(() => { period.value = 'day' })

const { data: nodeList, refresh: refreshNodes } = useFetch<NodeMetric[]>('/api/ops/nodes', { default: () => [], server: false })
usePoll(refreshNodes, 2000)
const nodes = computed(() => sortNodes(nodeList.value))
// Пинг экзита - задержка его прямого выхода в sing-box.
const { data: outbounds } = useFetch<Array<{ name: string, delay: number | null }>>('/api/routes/outbounds', { default: () => [], server: false })
const pings = computed(() => {
  const out: Record<string, number | null> = {}
  for (const o of outbounds.value) {
    const p = parseOutbound(o.name)
    if (p?.variant === 'direct') out[p.tag] = o.delay || null
  }
  return out
})

const { data: events, refresh: refreshEvents } = useFetch<PanelEvent[]>('/api/events', { query: { limit: 30 }, default: () => [] })
usePoll(refreshEvents, 30_000)
const now = ref(Date.now())
watch(events, () => { now.value = Date.now() })
const LEVEL: Record<EventLevel, string> = { alarm: 'var(--red)', warn: 'var(--amber)', info: 'var(--g2)', ok: 'var(--green)' }

// Выходы: доля экзита в текущем трафике; лидер акцентом, медленные и пропавшие красным.
const GREYS = ['var(--g1)', 'var(--g2)', 'var(--g3)', 'var(--g4)']
const exits = computed(() => {
  const list = nodes.value.filter(n => n.tag !== 'ru' && !n.disabled)
    .map(n => ({ tag: n.tag, v: Math.max(n.rxMbps ?? 0, n.txMbps ?? 0), bad: nodeHealth(n).kind !== 'ok' }))
    .sort((a, b) => Number(a.bad) - Number(b.bad) || b.v - a.v)
  const total = list.reduce((s, x) => s + x.v, 0)
  return { total, rows: list.map((x, i) => ({ ...x, c: x.bad ? 'var(--red)' : i === 0 ? 'var(--acc)' : GREYS[(i - 1) % 4]!, pct: total ? Math.round(x.v / total * 100) : 0 })) }
})

const { data: dns, refresh: refreshDns } = useFetch<{ available: boolean, totalToday: number, blockedToday: number, blockRatePct: number, avgMs: number }>('/api/ops/adguard', { server: false })
usePoll(refreshDns, 30_000)

const { data: top } = useFetch<{ clients: { id: number, name: string, rx: number, tx: number, total: number }[] }>('/api/clients/top', { query: { period }, server: false })
const topRows = computed(() => (top.value?.clients.filter(c => c.total > 0) ?? []).slice(0, mobile.value ? 3 : undefined))

const { data: online, refresh: refreshOnline } = useFetch<{ total: number, online: number, hourly: number[] }>('/api/ops/clients-online', { server: false })
usePoll(refreshOnline, 60_000)
const onlineCards = computed(() => {
  const d = online.value
  const share = (v: number) => d?.total ? `${Math.round(v / d.total * 100)}%` : '0%'
  return [
    { title: 'Клиенты онлайн', value: d ? ru(d.online) : '—', share: d ? share(d.online) : '—', data: d?.hourly ?? [] },
    { title: 'Клиенты офлайн', value: d ? ru(d.total - d.online) : '—', share: d ? share(d.total - d.online) : '—', data: d?.hourly.map(v => Math.max(0, d.total - v)) ?? [] },
  ]
})
</script>

<template>
  <main class="tab on ov">
    <div class="head">
      <button type="button" class="pill ml-auto" @click="period = period === 'day' ? 'week' : 'day'">
        {{ period === 'day' ? 'Сутки' : 'Неделя' }}
      </button>
    </div>
    <section class="grid gap-3 lg:grid-cols-[1.2fr_1.6fr_1fr_1fr]">
      <div class="card flex max-h-[290px] flex-col">
        <h3>События</h3>
        <ul v-if="events.length" class="scroll mt-3 flex-1 space-y-3">
          <li v-for="e in events" :key="e.id" class="grid grid-cols-[44px_8px_1fr] gap-x-2.5 text-sm">
            <time class="mono text-xs text-(--t3)" :datetime="new Date(e.at).toISOString()">{{ timeOrDdmmMsk(e.at, now) }}</time>
            <span class="dot mt-1.5" :style="{ background: LEVEL[e.level] }" />
            <div class="min-w-0 break-words">
              <b class="font-medium">{{ e.title }}</b><small v-if="e.detail" class="block text-xs text-(--t3)">{{ e.detail }}</small>
            </div>
          </li>
        </ul>
        <p v-else class="mt-3 text-sm text-(--t3)">
          Событий пока нет
        </p>
      </div>
      <OverviewWave :period="period" />
      <div class="card">
        <h3>Выходы</h3>
        <RingChart :segs="exits.rows">
          <b class="text-2xl font-light">{{ nodes.length ? ru(exits.total) : '—' }}</b><span class="text-xs text-(--t3)">Мбит/с сейчас</span>
        </RingChart>
        <div v-for="r in exits.rows" :key="r.tag" class="flex items-center gap-2 text-sm">
          <span class="dot" :style="{ background: r.c }" /><span class="flex-1 truncate">{{ exitName(r.tag) }}</span><em class="not-italic">{{ ru(r.v, 1) }}</em><i class="w-9 text-right text-(--t3) not-italic">{{ r.pct }}%</i>
        </div>
      </div>
      <div class="card">
        <h3>DNS сегодня</h3>
        <RingChart :segs="dns?.available ? [{ v: dns.blockedToday, c: 'var(--acc)' }, { v: dns.totalToday - dns.blockedToday, c: 'var(--g5)' }] : []">
          <b class="text-2xl font-light">{{ dns?.available ? `${ru(dns.blockRatePct, 1)}%` : '—' }}</b><span class="text-xs text-(--t3)">заблокировано</span>
        </RingChart>
        <div v-for="[label, v] in [['Заблокировано', dns?.available ? ru(dns.blockedToday) : '—'], ['Запросов', dns?.available ? ru(dns.totalToday) : '—'], ['Ответ в среднем', dns?.available ? `${ru(dns.avgMs, 1)} мс` : '—']]" :key="label" class="flex justify-between text-sm">
          <span class="text-(--t2)">{{ label }}</span><em class="not-italic">{{ v }}</em>
        </div>
      </div>
    </section>
    <OverviewNodes :nodes="nodes" :pings="pings" :refresh="refreshNodes" />
    <section class="grid gap-3 lg:grid-cols-[2fr_1.2fr_1fr]">
      <OverviewActivity />
      <div class="card flex max-h-[272px] flex-col">
        <h3>Топ клиентов</h3>
        <div class="scroll mt-2 flex-1">
          <NuxtLink v-for="(c, i) in topRows" :key="c.id" :to="{ path: '/clients', query: { id: c.id } }" class="grid grid-cols-[18px_minmax(0,1fr)_auto] gap-x-2 gap-y-1 rounded-lg p-2 text-sm hover:bg-(--chip)">
            <span class="text-xs text-(--t3)">{{ i + 1 }}</span><span class="truncate">{{ c.name }}</span><b class="font-medium">{{ sizeBinRu(c.total) }}</b>
            <span class="col-start-2 h-1 rounded bg-(--line)"><i class="block h-full rounded" :class="i ? 'bg-(--g3)' : 'bg-(--acc)'" :style="{ width: `${Math.max(2, c.total / topRows[0]!.total * 100)}%` }" /></span>
            <span class="text-xs text-(--t3)">↓ {{ sizeBinRu(c.rx) }} · ↑ {{ sizeBinRu(c.tx) }}</span>
          </NuxtLink>
          <p v-if="top && !topRows.length" class="py-8 text-center text-sm text-(--t3)">
            Трафика пока нет
          </p>
        </div>
      </div>
      <div class="flex flex-col gap-3">
        <div v-for="c in onlineCards" :key="c.title" class="card">
          <h3>{{ c.title }}</h3>
          <div class="my-2 flex items-end justify-between">
            <span class="big">{{ c.value }}</span><span class="tag">{{ c.share }}</span>
          </div>
          <LineChart :data="c.data" :h="50" :band="peakBand(c.data)" />
        </div>
      </div>
    </section>
  </main>
</template>
