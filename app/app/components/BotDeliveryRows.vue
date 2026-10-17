<script setup lang="ts">
import { botDeliveryText, botDeliveryTime, botQueueText, type BotDelivery } from '#shared/bot-delivery'

defineProps<{ queued: number, pending: BotDelivery[] }>()
</script>

<template>
  <div>
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
      <div>
        <span><b>{{ row.label }}</b>{{ botDeliveryText(row).slice(row.label.length) }}</span>
        <small v-if="row.created">{{ botDeliveryTime(row.created) }}</small>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bi small{display:block;color:var(--t3);font-size:11px;margin-top:2px}
</style>
