<script setup lang="ts">
import { botDeliveryText, botQueueText, type BotDelivery } from '#shared/bot-delivery'

const { rows, queued, sync } = useBotDeliveryToast()
const pending = computed(() => rows.value.filter(row => row.status !== 'accepted'))
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
  <UPopover v-if="pending.length">
    <UButton color="warning" variant="soft" icon="i-lucide-bell" :aria-label="`Уведомления: ${pending.length}`">
      <span class="hidden sm:inline">Уведомления:</span> {{ pending.length }}
    </UButton>
    <template #content>
      <ul class="max-h-80 max-w-sm overflow-auto p-4 space-y-2 text-sm">
        <li v-if="queued">{{ botQueueText(queued) }}</li>
        <li v-for="row in pending" :key="row.id">
          {{ botDeliveryText(row) }}
        </li>
      </ul>
    </template>
  </UPopover>
</template>
