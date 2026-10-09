<script setup lang="ts">
import type { Client, ClientTraffic } from '~/composables/useClients'
import { expiryLabel, fmtBytes } from '~/composables/useClients'
import { flagFor } from '~/composables/useRoutes'

const props = defineProps<{
  client: Client
  traffic?: ClientTraffic | null
  /** Теги экзитов для выпадашки предпочитаемого экзита. */
  exits?: string[]
}>()
const emit = defineEmits<{ open: [id: number], prefer: [id: number, tag: string | null] }>()

// Предпочитаемый экзит: «auto» — значение «авто» в USelect (null он не держит).
const AUTO = 'auto'
const preferItems = computed(() => [
  { label: '🌐', value: AUTO },
  ...(props.exits ?? []).map(t => ({ label: flagFor(t), value: t })),
])
const preferValue = computed(() => props.client.preferredExit ?? AUTO)
const preferTip = computed(() => {
  const t = props.client.preferredExit
  return t
    ? `Предпочитаемый экзит: ${flagFor(t)} ${t.toUpperCase()}. Если выключен или недоступен — трафик идёт по общим правилам. OpenVPN не учитывается.`
    : 'Предпочитаемый экзит: авто (по общим правилам). Выберите флаг, чтобы клиент ходил через этот экзит, пока он доступен.'
})
function onPrefer(v: string) {
  const tag = v === AUTO ? null : v
  if (tag !== props.client.preferredExit) emit('prefer', props.client.id, tag)
}

// Live-трафик из /api/clients/traffic перекрывает снапшот rxTotal/txTotal.
const rx = computed(() => props.traffic?.rxBytes ?? props.client.rxTotal)
const tx = computed(() => props.traffic?.txBytes ?? props.client.txTotal)
const trafficTotal = computed(() => rx.value + tx.value)
const dayRx = computed(() => props.traffic?.dayRx ?? 0)
const dayTx = computed(() => props.traffic?.dayTx ?? 0)
const dayTotal = computed(() => dayRx.value + dayTx.value)
const online = computed(() => props.traffic?.online ?? false)
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
      <UTooltip
        :text="online
          ? `В сети · за 5 мин ${fmtBytes(traffic?.recentBytes)}`
          : 'Не в сети · за 5 мин нет трафика'"
      >
        <span class="relative flex size-2.5 shrink-0" data-testid="client-online">
          <span
            v-if="online"
            class="absolute inline-flex size-full rounded-full bg-emerald-400 opacity-60 animate-ping"
          />
          <span
            class="relative inline-flex size-2.5 rounded-full"
            :class="online ? 'bg-emerald-400' : 'bg-(--ui-text-dimmed)/40'"
          />
        </span>
      </UTooltip>
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
        <div class="text-xs text-(--ui-text-muted) flex flex-wrap gap-x-3 gap-y-0.5 items-center mt-0.5">
          <span>{{ expiryLabel(client.expiresAt) }}</span>
          <UTooltip text="Девайсы: добавлено / лимит">
            <span class="flex items-center gap-0.5">
              <UIcon name="i-lucide-smartphone" class="size-3 shrink-0" />
              {{ client.deviceCount }}/{{ client.deviceLimit === null ? '∞' : client.deviceLimit }}
            </span>
          </UTooltip>
          <UTooltip :text="`За сутки (с 00:00 МСК) · ↓ ${fmtBytes(dayRx)} ↑ ${fmtBytes(dayTx)}`">
            <span
              class="flex items-center gap-0.5"
              :class="dayTotal > 0 ? 'text-(--ui-text)' : ''"
            >
              <UIcon name="i-lucide-arrow-down-up" class="size-3 shrink-0" />
              {{ fmtBytes(dayTotal) }}
            </span>
          </UTooltip>
          <UTooltip
            v-if="trafficTotal > 0"
            :text="`Трафик за всё время (все девайсы) · ↓ ${fmtBytes(rx)} ↑ ${fmtBytes(tx)}`"
          >
            <span class="text-(--ui-text-dimmed) whitespace-nowrap">всего {{ fmtBytes(trafficTotal) }}</span>
          </UTooltip>
        </div>
      </div>
      <UTooltip v-if="(exits ?? []).length" :text="preferTip">
        <div class="shrink-0" @click.stop @keydown.stop>
          <USelect
            :model-value="preferValue"
            :items="preferItems"
            size="xs"
            variant="ghost"
            class="w-14"
            :aria-label="`Предпочитаемый экзит клиента ${client.name}`"
            data-testid="client-preferred-exit"
            @update:model-value="onPrefer"
          />
        </div>
      </UTooltip>
      <UBadge
        v-if="client.status === 'frozen'"
        color="neutral"
        variant="subtle"
        size="sm"
      >
        Заморожен
      </UBadge>
    </div>
  </div>
</template>
