<script setup lang="ts">
// Сводка DNS: цифры, график за сутки, серверы выше и частые — всё из API AdGuard.
interface Summary {
  units: 'hours' | 'days'
  queries: number
  blocked: number
  blockedPct: number
  threats: number
  avgMs: number
  cachedPct: number | null
  q: number[]
  b: number[]
  t: number[]
  ms: number[]
  upstreams: Array<{ url: string, pct: number, ms: number }>
  mode: string
  clients: Array<[string, number]>
  domains: Array<[string, number]>
  blocks: Array<[string, number]>
}

const root = ref<HTMLElement>()
const { data, error, refresh } = useFetch<Summary>('/api/dns/summary', { server: false, lazy: true })
// Первые цифры приходят после показа раздела — тогда и лесенка карточек.
watch(data, (v, old) => {
  if (v && !old) void nextTick(() => cascade(root.value))
})
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => void refresh(), 60_000) })
onUnmounted(() => clearInterval(timer))
useVisibleRefresh(refresh)

const dec = (v: number) => (Math.round(v * 10) / 10).toString().replace('.', ',')
const ms = (v: number) => v < 10 ? dec(v) : String(Math.round(v))
const period = computed(() => data.value?.units === 'days' ? `за ${data.value.q.length} дн.` : 'за сутки')
const upMax = computed(() => Math.max(1, ...(data.value?.upstreams ?? []).map(u => u.pct)))
const lists = computed(() => data.value
  ? [
      { title: 'Частые клиенты', unit: 'запросов', rows: data.value.clients, red: false },
      { title: 'Частые запросы', unit: 'раз', rows: data.value.domains, red: false },
      { title: 'Частые блокировки', unit: 'раз', rows: data.value.blocks, red: true },
    ]
  : [])
</script>

<template>
  <div ref="root">
    <div v-if="error && !data" class="card">
      <h3>{{ (error.data as { message?: string } | undefined)?.message ?? error.statusMessage ?? 'AdGuard недоступен' }}</h3>
    </div>
    <template v-else-if="data">
      <div class="dk">
        <div class="card">
          <h3>Запросы DNS</h3>
          <div class="nums">
            <span class="big">{{ grp(data.queries) }}</span><span class="tag">{{ period }}</span>
          </div>
          <DnsSpark :data="data.q" />
        </div>
        <div class="card lit">
          <h3>Заблокировано фильтрами</h3>
          <div class="nums">
            <span class="big">{{ grp(data.blocked) }}</span><span class="tag">{{ dec(data.blockedPct) }}%</span>
          </div>
          <DnsSpark :data="data.b" />
        </div>
        <div class="card">
          <h3>Заблокировано угроз</h3>
          <div class="nums">
            <span class="big">{{ grp(data.threats) }}</span><span class="tag badt">фишинг, вредонос</span>
          </div>
          <DnsSpark :data="data.t" col="var(--red-t)" />
        </div>
        <div class="card">
          <h3>Время ответа</h3>
          <div class="nums">
            <span class="big">{{ ms(data.avgMs) }}<small>мс</small></span>
            <span v-if="data.cachedPct !== null" class="tag">{{ data.cachedPct }}% из кэша</span>
          </div>
          <DnsSpark :data="data.ms.length ? data.ms : [0, 0]" />
        </div>
      </div>
      <div class="d2">
        <DnsQChart :q="data.q" :b="data.b" :units="data.units" />
        <div class="card ups">
          <div class="fhead">
            <h3>Серверы выше</h3><span class="tag">{{ data.mode }}</span>
          </div>
          <div class="upl">
            <div v-for="u in data.upstreams" :key="u.url" class="up">
              <div class="u1">
                <b class="mono">{{ u.url }}</b><span>{{ u.pct }}%</span><em>{{ ms(u.ms) }}<small style="font-size:11px;color:var(--t3)"> мс</small></em>
              </div>
              <div class="bar">
                <i :style="{ width: `${u.pct / upMax * 92}%` }" />
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="d3">
        <div v-for="l in lists" :key="l.title" class="card tl">
          <div class="fhead">
            <h3>{{ l.title }}</h3><span class="tag">{{ l.unit }}</span>
          </div>
          <ul :class="{ red: l.red }">
            <li v-for="[name, c] in l.rows" :key="name">
              <span>{{ name }}</span><b>{{ grp(c) }}</b><span class="tb"><i :style="{ width: `${c / (l.rows[0]?.[1] || 1) * 100}%` }" /></span>
            </li>
          </ul>
        </div>
      </div>
    </template>
  </div>
</template>
