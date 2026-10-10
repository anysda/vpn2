<script setup lang="ts">
import type { TopPeriod } from '~/composables/useClients'
import { PERIOD_META, fmtBytes, formatRuDate, useClientsTop, useTrafficPeriod } from '~/composables/useClients'

const emit = defineEmits<{ open: [id: number] }>()

// Тот же период управляет трафиком нод в «Мониторинге».
const period = useTrafficPeriod()
const { top } = useClientsTop(period)

const COLLAPSED = 10
const expanded = ref(false)
function toggleExpanded() { expanded.value = !expanded.value }
function setPeriod(p: TopPeriod) { period.value = p }

const rows = computed(() => top.value?.clients ?? [])
const shown = computed(() => (expanded.value ? rows.value : rows.value.slice(0, COLLAPSED)))
const leader = computed(() => rows.value[0]?.total || 1)
const counts = computed(() => top.value?.counts)

// Пока почасовая история короче периода — честно говорим, с какого числа считаем.
const partialSince = computed(() => {
  const t = top.value
  if (!t?.historyStart || t.historyStart <= t.since) return null
  return formatRuDate(new Date(t.historyStart * 1000).toISOString())
})

const PERIODS = (Object.keys(PERIOD_META) as TopPeriod[]).map(value => ({ value, label: PERIOD_META[value].button }))
</script>

<template>
  <UCard>
    <template #header>
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <div class="font-semibold">
          ТОП клиентов
        </div>
        <div class="flex items-center gap-3">
          <span class="text-xs text-(--ui-text-muted)">
            {{ PERIOD_META[period].note }}<template v-if="partialSince">
              · данные с {{ partialSince }}</template>
          </span>
          <div class="flex rounded-md border border-(--ui-border) overflow-hidden">
            <UButton
              v-for="p in PERIODS"
              :key="p.value"
              size="xs"
              :variant="period === p.value ? 'solid' : 'ghost'"
              :color="period === p.value ? 'primary' : 'neutral'"
              class="rounded-none"
              @click="setPeriod(p.value)"
            >
              {{ p.label }}
            </UButton>
          </div>
        </div>
      </div>
    </template>

    <div v-if="counts" class="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-4">
      <div class="rounded-md border border-(--ui-border) bg-(--ui-page) p-2.5">
        <div class="text-xs text-(--ui-text-muted) flex items-center gap-1.5">
          <span class="inline-flex size-2 rounded-full bg-emerald-400" />в сети сейчас
        </div>
        <div class="text-lg font-semibold text-(--ui-text-highlighted)">
          {{ counts.onlineNow }}
        </div>
      </div>
      <div class="rounded-md border border-(--ui-border) bg-(--ui-page) p-2.5">
        <div class="text-xs text-(--ui-text-muted)">
          активны за сутки
        </div>
        <div class="text-lg font-semibold text-(--ui-text-highlighted)">
          {{ counts.activeDay }}
        </div>
      </div>
      <div class="rounded-md border border-(--ui-border) bg-(--ui-page) p-2.5">
        <div class="text-xs text-(--ui-text-muted)">
          активны за неделю
        </div>
        <div class="text-lg font-semibold text-(--ui-text-highlighted)">
          {{ counts.activeWeek }}
        </div>
      </div>
      <div class="rounded-md border border-(--ui-border) bg-(--ui-page) p-2.5">
        <div class="text-xs text-(--ui-text-muted)">
          всего клиентов
        </div>
        <div class="text-lg font-semibold text-(--ui-text-highlighted)">
          {{ counts.total }}
        </div>
      </div>
    </div>

    <div
      v-if="!top"
      class="text-(--ui-text-muted) text-sm text-center py-6"
    >
      загружаю…
    </div>
    <div
      v-else-if="rows.length === 0"
      class="text-(--ui-text-muted) text-sm text-center py-6"
    >
      Нет трафика за период
    </div>
    <div v-else class="space-y-1">
      <button
        v-for="(c, i) in shown"
        :key="c.id"
        type="button"
        class="w-full grid grid-cols-[1.5rem_minmax(0,1fr)_auto] items-center gap-3 rounded-md px-2 py-1.5 text-left text-sm hover:bg-(--ui-bg-elevated) transition-colors"
        @click="emit('open', c.id)"
      >
        <span class="text-xs text-(--ui-text-dimmed) tabular-nums text-right">{{ i + 1 }}</span>
        <div class="min-w-0">
          <div class="truncate text-(--ui-text-highlighted)">
            {{ c.name }}
          </div>
          <div class="mt-1 h-1.5 rounded-full bg-(--ui-bg-elevated) overflow-hidden">
            <div
              class="h-full rounded-full bg-(--ui-primary)"
              :style="{ width: `${Math.max(2, (c.total / leader) * 100)}%` }"
            />
          </div>
        </div>
        <div class="text-right tabular-nums">
          <div class="font-medium text-(--ui-text-highlighted)">
            {{ fmtBytes(c.total) }}
          </div>
          <div class="text-xs text-(--ui-text-muted)">
            ↓ {{ fmtBytes(c.rx) }} ↑ {{ fmtBytes(c.tx) }}
          </div>
        </div>
      </button>
      <div v-if="rows.length > COLLAPSED" class="pt-1 text-center">
        <UButton
          size="xs"
          variant="ghost"
          color="neutral"
          :icon="expanded ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
          @click="toggleExpanded"
        >
          {{ expanded ? 'Свернуть' : `Показать всех (${rows.length})` }}
        </UButton>
      </div>
    </div>
  </UCard>
</template>
