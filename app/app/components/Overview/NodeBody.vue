<script setup lang="ts">
import type { NodeMetric } from '~/composables/useMonitoring'
import { count, drawSvg, ru } from '~/utils/overview'
import { exitName, level, nodeHealth, uptimeShort } from '~/utils/overview-nodes'

// Содержимое окна узла: подробности и выключение экзита. Одно и то же в
// модалке десктопа и в шторке телефона; подтверждение встаёт вместо подробностей.
const props = defineProps<{
  node: NodeMetric | null
  nodes: NodeMetric[]
  data: number[]
  open: boolean
  setExitDisabled: (tag: string, disabled: boolean) => Promise<void>
}>()
const asking = defineModel<boolean>('asking', { default: false })
const emit = defineEmits<{ close: [] }>()
const toast = useToast()

const ERRORS: Record<string, string> = {
  last_enabled_exit: 'Нельзя выключить последний включённый экзит',
  unknown_exit: 'Неизвестный экзит',
}

const busy = ref(false)
const n = computed(() => props.node)
const name = computed(() => n.value ? exitName(n.value.tag) : '')
const isExit = computed(() => Boolean(n.value && n.value.tag !== 'ru'))
const bad = computed(() => Boolean(n.value && nodeHealth(n.value).kind !== 'ok'))
const pct = (v: number | null | undefined) => v == null ? '—' : `${Math.round(v)}%`
const mbps = (v: number | null | undefined) => v == null ? '—' : ru(v, v < 10 ? 1 : 0)
const gb = (b: number | null | undefined) => b == null ? '—' : `${ru(b / 1e9, b < 1e10 ? 1 : 0)} ГБ`
const disk = computed(() => {
  const x = n.value
  if (!x || x.diskFreeBytes == null) return null
  const free = x.diskFreeBytes / 1024 ** 3, p = x.diskFreePct
  return { free: `${Math.round(free)} GB`, pct: p, size: p ? `${Math.round(free / p * 100)} GB` : '—' }
})

const body = ref<HTMLElement>()
watch([() => props.open, asking], async ([o, a]) => {
  if (!o || a) return
  await nextTick()
  requestAnimationFrame(() => {
    body.value?.querySelectorAll('.mgrid b').forEach((b, i) => count(b, 600, i * 25))
    drawSvg(body.value?.querySelector<SVGSVGElement>('.msp svg'), 100, 900)
  })
}, { immediate: true })

function onToggle() {
  const x = n.value
  if (!x || busy.value) return
  if (x.disabled) return void apply(x, false)
  if (props.nodes.every(y => y.tag === x.tag || y.tag === 'ru' || y.disabled)) {
    toast.add({ title: ERRORS.last_enabled_exit, color: 'error' })
    return
  }
  if (x.active || x.activeDevices) asking.value = true
  else void apply(x, true)
}

async function apply(x: NodeMetric, disabled: boolean) {
  busy.value = true
  try {
    await props.setExitDisabled(x.tag, disabled)
    toast.add({ title: `${exitName(x.tag)} ${disabled ? 'выключен' : 'включён'}`, color: 'success' })
    if (asking.value) emit('close')
  }
  catch (e) {
    const err = e as { statusMessage?: string, data?: { statusMessage?: string } }
    const code = err.data?.statusMessage ?? err.statusMessage ?? ''
    toast.add({ title: ERRORS[code] ?? (code || 'Ошибка'), color: 'error' })
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-if="n && asking" class="ov-ask">
    <div class="mh"><h2>Выключить {{ name }}?</h2></div>
    <p class="mt">Через {{ n.tag.toUpperCase() }} сейчас идёт трафик. Выключить? Watchdog уведёт его на другие экзиты, текущие соединения оборвутся.</p>
    <div class="mf">
      <button type="button" class="pill" @click="emit('close')">Отмена</button>
      <button type="button" class="pill redw" :disabled="busy" @click="apply(n, true)">Выключить</button>
    </div>
  </div>
  <div v-else-if="n" ref="body" class="ov-node">
    <div class="mh">
      <div><h2>{{ name }}</h2><span class="sub2 mono">{{ n.host ?? n.instance }} · аптайм {{ uptimeShort(n.uptimeSec) }}</span></div>
      <div v-if="isExit" class="ha" style="align-items:center">
        <span class="tag">{{ n.disabled ? 'ВЫКЛ' : `${n.activeDevices ?? 0} устр.` }}</span>
        <button
          type="button" class="tog" :class="{ on: !n.disabled }" role="switch" :aria-checked="!n.disabled"
          :aria-label="`${name}: ${n.disabled ? 'включить' : 'выключить'}`" :disabled="busy" @click="onToggle"
        ><i /></button>
      </div>
      <button v-else type="button" class="xb" aria-label="Закрыть" @click="emit('close')">
        <svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>
    <div class="mgrid">
      <div><span>CPU</span><b :class="level(n.cpu, 60, 80)">{{ pct(n.cpu) }}</b></div>
      <div><span>iowait</span><b :class="level(n.iowait, 10, 30)">{{ pct(n.iowait) }}</b></div>
      <div><span>steal</span><b :class="level(n.steal, 10, 30)">{{ pct(n.steal) }}</b></div>
      <div><span>RAM</span><b :class="level(n.ram, 60, 80)">{{ pct(n.ram) }}</b></div>
      <div title="Свободно на корневом разделе">
        <span>HDD свободно</span>
        <b :class="disk?.pct == null ? '' : disk.pct < 10 ? 'c' : disk.pct < 20 ? 'w' : ''">{{ disk?.free ?? '—' }}</b>
        <small v-if="disk">{{ disk.pct == null ? '' : `${Math.round(disk.pct)}% из ${disk.size}` }}</small>
      </div>
      <div><span>Полос</span><b>{{ n.lanes ?? 0 }}</b><small>warp {{ n.lanesWarp ?? 0 }}</small></div>
      <div><span>↓ сейчас</span><b>{{ mbps(n.rxMbps) }}</b><small>Mbps</small></div>
      <div><span>↑ сейчас</span><b>{{ mbps(n.txMbps) }}</b><small>Mbps</small></div>
      <div><span>Сутки ↓</span><b>{{ gb(n.rxTodayBytes) }}</b><small>сутки ↑ {{ gb(n.txTodayBytes) }}</small></div>
    </div>
    <OverviewLine class="msp" :data="data" :w="400" :h="70" :pad="6" :col="bad ? 'var(--red-t)' : 'var(--acc)'" />
    <div class="mf"><button type="button" class="pill" @click="emit('close')">Закрыть</button></div>
  </div>
</template>
