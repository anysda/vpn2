<script setup lang="ts">
import { botDeliveryText, botDeliveryTime, botQueueText } from '#shared/bot-delivery'

const { rows, queued, sync } = useBotDelivery()
const pending = computed(() => rows.value.filter(r => r.status !== 'accepted'))
const label = computed(() => pending.value.length ? `Уведомления: ${pending.value.length}` : 'Уведомления')
let first = true

async function refresh() {
  const list = await $fetch('/api/admin/bot-deliveries').catch(() => null)
  if (!list) return
  sync(list, !first)
  first = false
}
onMounted(refresh)
usePoll(refresh, 2000)
onUnmounted(() => sync([], false))
</script>

<template>
  <UPopover :content="{ align: 'end', sideOffset: 10 }" :ui="{ content: 'card w-[360px] max-w-[calc(100vw-24px)]' }">
    <button type="button" class="ib" :title="label" :aria-label="label">
      <svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20.5h4" /></svg>
      <em v-if="pending.length" class="bd" aria-hidden="true">{{ pending.length }}</em>
    </button>
    <template #content>
      <h4 class="px-2 pb-2 text-xs text-(--t3)">
        Доставка уведомлений
      </h4>
      <p v-if="!queued && !pending.length" class="px-2 py-1 text-sm">
        Все уведомления доставлены
      </p>
      <p v-if="queued" class="flex gap-2.5 px-2 py-1 text-sm">
        <span class="dot mt-1.5" style="background:var(--amber)" />{{ botQueueText(queued) }}
      </p>
      <div v-for="r in pending" :key="r.id" class="flex gap-2.5 px-2 py-1 text-sm">
        <span class="dot mt-1.5" :style="{ background: r.status === 'failed' ? 'var(--red)' : 'var(--amber)' }" />
        <div>
          <b class="font-medium">{{ r.label }}</b>{{ botDeliveryText(r).slice(r.label.length) }}
          <small v-if="r.created" class="block text-xs text-(--t3)">{{ botDeliveryTime(r.created) }}</small>
        </div>
      </div>
    </template>
  </UPopover>
</template>
