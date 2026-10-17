<script setup lang="ts">
import '~/assets/css/overview.css'
import { useScreenLayout } from '~/composables/useScreenLayout'
import { WAKE } from '~/utils/overview'
import { sortNodes } from '~/utils/overview-nodes'

useHead({ title: 'Обзор — anysda-vpn2' })
definePageMeta({ title: 'Обзор' })

// Первый показ Обзора за сессию — сцена «сеть просыпается»: блоки
// появляются по очереди. Дальше страница открывается сразу.
const woke = useState('ov-woke', () => false)
const wake = reactive({ on: false, t0: 0 })
provide(WAKE, wake)
const tab = ref<HTMLElement>()
// Телефон: блоки в разметке в порядке экрана (Tab и скринридер идут по ней).
const mobile = useMobile()
onMounted(async () => {
  if (woke.value) return
  // мобильная разметка встаёт после монтирования: анимировать уже её
  await nextTick()
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
// период графика трафика: сутки по часам или неделя по суткам; с Обзора уходит вместе с ним
const period = useOverviewPeriod()
onUnmounted(() => { period.value = 'day' })

// старое `events` (рассылка и лента) из сохранённых раскладок отсеивает
// normalizeScreenLayout: лента теперь в traffic, рассылка ушла с Обзора
const KNOWN = new Set(['traffic', 'nodes', 'activity'])
const blocks = computed(() => [...layout.value.left, ...layout.value.right].filter(id => KNOWN.has(id)))
// Телефон: блоки в порядке «Настроить экран», как на ПК. По макету телефона
// «Узлы», стоящие сразу за трафиком, встают между лентой и выходами.
const nodesInTraffic = computed(() => {
  const i = blocks.value.indexOf('traffic')
  return i >= 0 && blocks.value[i + 1] === 'nodes'
})
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
    <!-- телефон: модули в порядке экрана; в трафике события, выходы и DNS, трафик; ряды раскрыты в общую сетку -->
    <template v-if="mobile">
      <template v-for="id in blocks" :key="id">
        <template v-if="id === 'traffic'">
          <section class="r1" data-blk="traffic">
            <EventsFeed />
          </section>
          <OverviewNodes v-if="nodesInTraffic" data-blk="nodes" :nodes="sorted" :pings="pings" :set-exit-disabled="setExitDisabled" />
          <section class="r1">
            <OverviewExits :nodes="sorted" />
            <OverviewDns />
            <OverviewWave :period="period" />
          </section>
        </template>
        <OverviewNodes v-else-if="id === 'nodes' && !nodesInTraffic" data-blk="nodes" :nodes="sorted" :pings="pings" :set-exit-disabled="setExitDisabled" />
        <section v-else-if="id === 'activity'" class="ra" data-blk="activity">
          <OverviewActivity />
          <OverviewTop :period="period" />
          <OverviewClients />
        </section>
      </template>
    </template>
    <template v-else>
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
    </template>
  </main>
</template>
