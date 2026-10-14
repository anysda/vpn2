<script setup lang="ts">
import { botDeliveryText, botQueueText, type BotDelivery } from '#shared/bot-delivery'

const { rows, queued, sync } = useBotDeliveryToast()
const pending = computed(() => rows.value.filter(row => row.status !== 'accepted'))
const pop = useHeaderPop()
const open = computed(() => pop.value === 'bell')
const list = ref<HTMLElement>()

function toggle() {
  pop.value = open.value ? null : 'bell'
  if (!open.value || matchMedia('(prefers-reduced-motion: reduce)').matches) return
  // Строки проявляются лесенкой, как в макете.
  requestAnimationFrame(() => [...(list.value?.children ?? [])].slice(0, 6).forEach((row, i) =>
    row.animate([{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }],
      { duration: 240, delay: 40 + i * 30, easing: 'cubic-bezier(.23,1,.32,1)', fill: 'backwards' })))
}
let timer: ReturnType<typeof setInterval> | undefined
let running = false
let initialized = false
let disposed = false
const controller = new AbortController()
async function refresh() {
  if (running) return
  running = true
  try {
    const deliveries = await $fetch<BotDelivery[]>('/api/admin/bot-deliveries', { signal: controller.signal })
    if (disposed) return
    sync(deliveries, initialized)
    initialized = true
  }
  catch { /* При разрыве связи история останется в панели до следующего опроса. */ }
  finally { running = false }
}
onMounted(() => {
  void refresh()
  timer = setInterval(refresh, 2000)
})
onUnmounted(() => {
  disposed = true
  controller.abort()
  clearInterval(timer)
  sync([], false)
})
</script>

<template>
  <div class="pw">
    <button
      type="button"
      class="ib"
      :title="pending.length ? `Уведомления: ${pending.length}` : 'Уведомления'"
      :aria-label="pending.length ? `Уведомления: ${pending.length}` : 'Уведомления'"
      :aria-expanded="open"
      @click.stop="toggle"
    >
      <svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20.5h4" /></svg>
      <em v-if="pending.length" class="bd" aria-hidden="true">{{ pending.length }}</em>
    </button>
    <div
      class="card pop"
      :class="{ on: open }"
      role="dialog"
      aria-label="Доставка уведомлений"
      :aria-hidden="!open"
      :inert="!open"
    >
      <h4>Доставка уведомлений</h4>
      <div ref="list">
        <div v-if="!queued && !pending.length" class="bi">
          <span />
          <div>Все уведомления доставлены</div>
        </div>
        <div v-if="queued" class="bi">
          <span class="dot" style="background:var(--amber)" />
          <div>{{ botQueueText(queued) }}</div>
        </div>
        <div v-for="row in pending" :key="row.id" class="bi">
          <span class="dot" :style="{ background: row.status === 'failed' ? 'var(--red)' : 'var(--amber)' }" />
          <div><b>{{ row.label }}</b>{{ botDeliveryText(row).slice(row.label.length) }}</div>
        </div>
      </div>
    </div>
  </div>
</template>
