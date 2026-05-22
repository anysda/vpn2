<script setup lang="ts">
import type { Client } from '~/composables/useClients'
import { expiryLabel, fmtBytes } from '~/composables/useClients'

const props = defineProps<{
  client: Client
  traffic?: { rxBytes: number, txBytes: number } | null
}>()
const emit = defineEmits<{ open: [id: number] }>()

// Live-трафик из /api/clients/traffic перекрывает снапшот rxTotal/txTotal.
const rx = computed(() => props.traffic?.rxBytes ?? props.client.rxTotal)
const tx = computed(() => props.traffic?.txBytes ?? props.client.txTotal)
const trafficTotal = computed(() => rx.value + tx.value)
</script>

<template>
  <div
    class="rounded-md border border-(--ui-border) bg-(--ui-page) px-3 py-2 cursor-pointer transition-colors hover:border-(--ui-border-accented)"
    role="button"
    tabindex="0"
    @click="emit('open', client.id)"
    @keydown.enter="emit('open', client.id)"
    @keydown.space.prevent="emit('open', client.id)"
  >
    <div class="flex items-center gap-3">
      <div class="flex-1 min-w-0">
        <div class="font-medium text-(--ui-text-highlighted) leading-tight flex items-center gap-1.5">
          <UTooltip
            v-if="client.filterTraffic"
            text="Фильтрация рекламы и трекеров включена"
          >
            <UIcon
              name="i-simple-icons-adguard"
              class="size-4 text-[#67b279] shrink-0"
            />
          </UTooltip>
          <span class="truncate">{{ client.name }}</span>
          <UTooltip v-if="client.tgLinked" text="Привязан к Telegram">
            <UIcon name="i-simple-icons-telegram" class="size-3.5 text-[#26A5E4] shrink-0" />
          </UTooltip>
        </div>
        <div class="text-xs text-(--ui-text-muted) flex gap-3 items-center mt-0.5">
          <span>{{ expiryLabel(client.expiresAt) }}</span>
          <UTooltip
            v-if="trafficTotal > 0"
            :text="`Трафик за всё время (все девайсы) · ↓ ${fmtBytes(rx)} ↑ ${fmtBytes(tx)}`"
          >
            <span class="flex items-center gap-0.5">
              <UIcon name="i-lucide-arrow-down" class="size-3 shrink-0" />
              {{ fmtBytes(trafficTotal) }}
            </span>
          </UTooltip>
          <UTooltip text="Девайсы: добавлено / лимит">
            <span class="flex items-center gap-0.5">
              <UIcon name="i-lucide-smartphone" class="size-3 shrink-0" />
              {{ client.deviceCount }}/{{ client.deviceLimit === null ? '∞' : client.deviceLimit }}
            </span>
          </UTooltip>
        </div>
      </div>
      <UBadge
        :color="client.status === 'active' ? 'success' : 'neutral'"
        variant="subtle"
        size="sm"
      >
        {{ client.status === 'active' ? 'Активен' : 'Заморожен' }}
      </UBadge>
    </div>
  </div>
</template>
