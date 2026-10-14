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
  const r4 = tab.value?.querySelector('.r4')
  r4?.animate([{ opacity: 0, transform: 'translateY(14px)' }, { opacity: 1, transform: 'none' }],
    { duration: 600, delay: 1150, easing: 'cubic-bezier(.23,1,.32,1)', fill: 'backwards' })
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
const KNOWN = new Set(['traffic', 'nodes', 'activity', 'events'])
const blocks = computed(() => [...layout.value.left, ...layout.value.right].filter(id => KNOWN.has(id)))
</script>

<template>
  <main ref="tab" class="tab on ov" :data-wake="!woke || wake.on ? '' : undefined">
    <template v-for="id in blocks" :key="id">
      <section v-if="id === 'traffic'" class="r1">
        <div class="card wave"><h3>Трафик за сутки</h3></div>
        <OverviewExits :nodes="sorted" />
        <OverviewDns />
      </section>
      <OverviewNodes v-else-if="id === 'nodes'" :nodes="sorted" :pings="pings" :set-exit-disabled="setExitDisabled" />
      <section v-else-if="id === 'activity'" class="ra">
        <div class="card act"><div class="fhead"><h3>Активность</h3></div></div>
        <OverviewClients />
      </section>
      <section v-else-if="id === 'events'" class="r4">
        <OverviewBroadcast />
        <EventsFeed />
      </section>
    </template>
  </main>
</template>
