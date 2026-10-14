<script setup lang="ts">
import type { NodeMetric } from '~/composables/useMonitoring'
import { anim } from '~/utils/clients-view'
import { useWake } from '~/utils/overview'
import { exitName, nodeHealth, nodeSubtitle } from '~/utils/overview-nodes'

// Узлы: entry и экзиты карточками. Спарклайн — CPU за последние 13 опросов:
// начало из VM, дальше каждый опрос /api/ops/nodes дописывает точку справа.
const props = defineProps<{
  nodes: NodeMetric[]
  pings: Record<string, number | null>
  setExitDisabled: (tag: string, disabled: boolean) => Promise<void>
}>()

const POINTS = 13
const series = ref<Record<string, number[]>>({})
let seeded = false

// пропуски VM — соседним значением, чтобы линия не рвалась
function fillGaps(raw: Array<number | null>): number[] {
  const first = raw.find(v => v != null)
  if (first == null) return []
  let last = first
  return raw.map(v => (last = v ?? last))
}

onMounted(async () => {
  const seed = await $fetch<Record<string, Array<number | null>>>('/api/ops/nodes-cpu').catch(() => ({}))
  const next: Record<string, number[]> = {}
  for (const [tag, raw] of Object.entries(seed)) next[tag] = fillGaps(raw)
  series.value = { ...next, ...series.value }
  seeded = true
})

watch(() => props.nodes, (list) => {
  if (!seeded) return
  const next = { ...series.value }
  for (const n of list) {
    if (n.cpu == null || nodeHealth(n).kind === 'offline') continue
    const h = next[n.tag] ?? []
    next[n.tag] = [...(h.length >= POINTS ? h.slice(1) : h), n.cpu]
  }
  series.value = next
})

const cards = computed(() => props.nodes.map((n) => {
  const h = nodeHealth(n)
  return { n, h, name: exitName(n.tag), sub: nodeSubtitle(n, props.pings[n.tag]), data: series.value[n.tag] ?? [] }
}))

const box = ref<HTMLElement>()
const wake = useWake()
let entered = false
watch(() => props.nodes.length, (len) => {
  if (entered || !len) return
  entered = true
  if (!wake.on) return
  nextTick(() => [...(box.value?.children ?? [])].forEach((c, i) =>
    anim(c, [{ opacity: 0, transform: 'translateY(14px)' }, { opacity: c.classList.contains('off') ? 0.5 : 1, transform: 'none' }], { duration: 560, delay: wake.at(520 + i * 90) })))
}, { immediate: true })

const openTag = ref<string | null>(null)
const open = computed({ get: () => openTag.value != null, set: (v) => { if (!v) openTag.value = null } })
const opened = computed(() => props.nodes.find(n => n.tag === openTag.value) ?? null)
</script>

<template>
  <section ref="box" class="r3 ov-nodes" :style="{ '--n': Math.min(Math.max(nodes.length, 1), 5) }">
    <div
      v-for="(c, i) in cards" :key="c.n.tag"
      class="card node" :class="{ lit: c.h.lit && !c.n.disabled, off: c.n.disabled }"
      role="button" tabindex="0" :aria-label="`${c.name}: подробности`" :data-tag="c.n.tag"
      @click="openTag = c.n.tag" @keydown.enter.prevent="openTag = c.n.tag" @keydown.space.prevent="openTag = c.n.tag"
    >
      <div class="nt">
        <div>{{ c.name }}<small>{{ c.sub }}</small></div>
        <span class="ar"><svg class="i" viewBox="0 0 24 24"><path d="M8 16L16 8M9 8h7v7" /></svg></span>
      </div>
      <div class="cpu">
        <OverviewNum class="big" :value="c.h.big" :dur="800" :delay="520 + i * 90 + 160" /><span>{{ c.h.bigLabel }}</span>
      </div>
      <OverviewLine
        class="sp" :data="c.data" :w="200" :h="44" :pad="4"
        :col="c.h.kind === 'ok' ? 'var(--acc)' : 'var(--red-t)'"
        :live="c.h.kind === 'ok' ? true : 'bad'" :live-idx="2 + i" :draw="[520 + i * 90 + 160, 800]"
      />
      <div class="ft">
        <span class="tag" :class="c.n.disabled ? '' : c.h.kind === 'ok' ? 'ok' : 'badt'">{{ c.n.disabled ? 'ВЫКЛ' : c.h.tag }}</span>
        <span class="m">{{ c.h.meta }}</span>
      </div>
    </div>
    <OverviewNodeModal
      v-model:open="open" :node="opened" :nodes="nodes" :data="opened ? series[opened.tag] ?? [] : []"
      :set-exit-disabled="setExitDisabled"
    />
  </section>
</template>
