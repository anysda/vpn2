<script setup lang="ts">
import '~/assets/css/overview.css'
import { useScreenLayout } from '~/composables/useScreenLayout'
import { WAKE } from '~/utils/overview'
import { sortNodes } from '~/utils/overview-nodes'

useHead({ title: 'Обзор — anysda-vpn2' })

// Первый показ Обзора за сессию — сцена «сеть просыпается»: блоки
// появляются по очереди. Дальше страница открывается сразу.
const woke = useState('ov-woke', () => false)
const wake = reactive({ on: false, t0: 0 })
provide(WAKE, wake)
const tab = ref<HTMLElement>()
onMounted(() => {
  if (woke.value) return
  woke.value = true
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return
  wake.on = true
  wake.t0 = performance.now()
  const ev = tab.value?.querySelector('.r1>.ev')
  ev?.animate([{ opacity: 0, transform: 'translateY(14px)' }, { opacity: 1, transform: 'none' }],
    { duration: 600, delay: 120, easing: 'cubic-bezier(.23,1,.32,1)', fill: 'backwards' })
  setTimeout(() => { wake.on = false }, 3000)
})

const { nodes, setExitDisabled } = useMonitoring()
const sorted = computed(() => sortNodes(nodes.value ?? []))

// пинг экзита — задержка его прямого выхода в sing-box
const { data: outbounds } = useFetch<Array<{ name: string, delay: number | null }>>('/api/routes/outbounds', { server: false })
const pings = computed(() => {
  const out: Record<string, number | null> = {}
  for (const o of outbounds.value ?? []) {
    const m = /^hy2-(.+)-direct$/.exec(o.name)
    if (m) out[m[1]!] = o.delay || null
  }
  return out
})

const { layout } = useScreenLayout()
// период графика трафика: сутки по часам или неделя по суткам
const period = ref<'day' | 'week'>('day')

// старое `events` (рассылка и лента) из сохранённых раскладок отсеивает
// normalizeScreenLayout: лента теперь в traffic, рассылка ушла с Обзора
const KNOWN = new Set(['traffic', 'nodes', 'activity'])
const blocks = computed(() => [...layout.value.left, ...layout.value.right].filter(id => KNOWN.has(id)))
</script>

<template>
  <main ref="tab" class="tab on ov" :data-wake="!woke || wake.on ? '' : undefined">
    <div class="head">
      <div class="ha" style="margin-left:auto">
        <button type="button" class="pill" @click="period = period === 'day' ? 'week' : 'day'">
          <svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M7 12h10M10 18h4" /></svg>{{ period === 'day' ? 'Сутки' : 'Неделя' }}
        </button>
      </div>
    </div>
    <template v-for="id in blocks" :key="id">
      <section v-if="id === 'traffic'" class="r1" data-blk="traffic">
        <EventsFeed />
        <OverviewWave :period="period" />
        <OverviewExits :nodes="sorted" />
        <OverviewDns />
      </section>
      <OverviewNodes v-else-if="id === 'nodes'" data-blk="nodes" :nodes="sorted" :pings="pings" :set-exit-disabled="setExitDisabled" />
      <section v-else-if="id === 'activity'" class="ra" data-blk="activity">
        <OverviewActivity />
        <OverviewTop :period="period" />
        <OverviewClients />
      </section>
    </template>
  </main>
</template>
