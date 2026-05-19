<script setup lang="ts">
interface AghResponse {
  available: boolean
  totalToday?: number
  blockedToday?: number
  blockRatePct?: number
  avgMs?: number
  topBlocked?: { domain: string, count: number } | null
}

const { data, refresh } = useFetch<AghResponse>('/api/ops/adguard', {
  default: () => ({ available: false }),
  server: false,
})

let timer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  if (!timer) timer = setInterval(() => { void refresh() }, 60_000)
})
onUnmounted(() => { if (timer) { clearInterval(timer); timer = null } })
useVisibleRefresh(refresh)

const aghOrigin = computed(() => {
  if (typeof window === 'undefined') return ''
  return `${window.location.protocol}//${window.location.hostname}:3001`
})

function fmt(n: number | undefined | null): string {
  if (n == null) return '—'
  return new Intl.NumberFormat('ru-RU').format(Math.floor(n))
}
</script>

<template>
  <UCard>
    <template #header>
      <div class="font-semibold">
        DNS
      </div>
    </template>

    <div class="space-y-4">
      <div v-if="!data?.available" class="text-(--ui-text-muted) text-sm text-center py-3">
        AdGuard недоступен (нет соединения / неверные креды)
      </div>
      <div v-else class="grid grid-cols-3 gap-3 text-sm">
        <div>
          <div class="text-(--ui-text-muted) text-xs">
            Запросов сегодня
          </div>
          <div class="font-semibold">
            {{ fmt(data.totalToday) }}
          </div>
        </div>
        <div>
          <div class="text-(--ui-text-muted) text-xs">
            Заблокировано
          </div>
          <div class="font-semibold">
            {{ fmt(data.blockedToday) }}
            <span class="text-(--ui-text-muted) text-xs font-normal">
              ({{ (data.blockRatePct ?? 0).toFixed(1) }}%)
            </span>
          </div>
        </div>
        <div>
          <div class="text-(--ui-text-muted) text-xs">
            Avg мс
          </div>
          <div class="font-semibold">
            {{ (data.avgMs ?? 0).toFixed(1) }}
          </div>
        </div>
        <div v-if="data.topBlocked" class="col-span-3 text-xs text-(--ui-text-muted)">
          Топ блокировки:
          <span class="text-(--ui-text)">{{ data.topBlocked.domain }}</span>
          <span class="ml-1">({{ data.topBlocked.count }})</span>
        </div>
      </div>

      <a
        v-if="aghOrigin"
        :href="aghOrigin"
        target="_blank"
        rel="noopener"
        class="flex items-center justify-center gap-2 w-full py-3 rounded-md bg-[#67B279] hover:bg-[#5BA56C] text-white font-semibold text-base transition-colors"
      >
        <UIcon name="i-simple-icons-adguard" class="size-5" />
        AdGuard Home
        <UIcon name="i-lucide-external-link" class="size-4 opacity-80" />
      </a>
    </div>
  </UCard>
</template>
