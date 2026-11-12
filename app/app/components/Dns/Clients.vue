<script setup lang="ts">
import { plural } from '#shared/plural'

const { data, load } = useDns('/api/dns/clients')
const tag = computed(() => {
  const n = data.value?.length ?? 0, ips = (data.value ?? []).reduce((a, r) => a + r.ips.length, 0)
  return `${n} ${plural(n, 'client')} · ${ips} ${plural(ips, 'address')}`
})
const ips = (l: string[]) => l.length ? l.slice(0, 2).join(', ') + (l.length > 2 ? ` +${l.length - 2}` : '') : '—'
const SAFE = { on: ['ok', 'включён'], off: ['', 'выключен'], global: ['', 'как у всех'] }
const COLS = 'grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_100px_130px_minmax(0,1fr)_80px] items-center gap-3 px-1'
</script>

<template>
  <div>
    <DnsLoadError :of="load" />
    <div class="card overflow-x-auto">
      <DnsHead title="Клиенты DNS" sub="Узнаются по адресу в туннеле. Настройки здесь сильнее общих">
        <span v-if="data" class="tag">{{ tag }}</span>
      </DnsHead>
      <div class="min-w-[760px]">
        <div class="py-2 text-xs text-(--t3)" :class="COLS">
          <span>Клиент</span><span>Адреса</span><span>Фильтры</span><span>Безопасный поиск</span><span>Сервисы</span><span>Запросов</span>
        </div>
        <div v-for="c in data ?? []" :key="c.name" data-testid="dns-client-row" class="border-t border-(--line) py-2.5 text-sm" :class="COLS">
          <div class="who">
            <span class="av">{{ initials(c.name) }}</span>
            <div class="min-w-0">
              <b class="block truncate font-medium">{{ c.name }}</b><small class="text-xs text-(--t3)">{{ c.tg }}</small>
            </div>
          </div>
          <span class="mono text-(--t2)">{{ ips(c.ips) }}</span>
          <span><span class="tag" :class="{ ok: c.filters === 'common' }">{{ c.filters === 'common' ? 'общие' : 'выключены' }}</span></span>
          <span><span class="tag" :class="SAFE[c.safesearch][0]">{{ SAFE[c.safesearch][1] }}</span></span>
          <span :class="{ 'text-(--t3)': !c.services }">{{ c.services ?? 'как у всех' }}</span>
          <span>{{ ru(c.queries) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
