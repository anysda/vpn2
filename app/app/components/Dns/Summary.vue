<script setup lang="ts">
const { data, error, refresh } = useFetch('/api/dns/summary', { server: false, lazy: true })
usePoll(refresh, 60_000)

const ms = (v: number) => v < 10 ? ru(v, 1) : ru(v)
const period = computed(() => data.value?.units === 'days' ? `за ${data.value.q.length} дн.` : 'за сутки')
const upMax = computed(() => Math.max(1, ...(data.value?.upstreams ?? []).map(u => u.pct)))
const cards = computed(() => {
  const d = data.value!
  return [
    { title: 'Запросы DNS', big: ru(d.queries), tag: period.value, line: d.q },
    { title: 'Заблокировано фильтрами', big: ru(d.blocked), tag: `${ru(d.blockedPct, 1)}%`, line: d.b, lit: true },
    { title: 'Заблокировано угроз', big: ru(d.threats), tag: 'фишинг, вредонос', line: d.t, red: true },
    { title: 'Время ответа', big: `${ms(d.avgMs)} мс`, tag: d.cachedPct === null ? '' : `${d.cachedPct}% из кэша`, line: d.ms },
  ]
})
const lists = computed(() => [
  { title: 'Частые клиенты', unit: 'запросов', rows: data.value!.clients },
  { title: 'Частые запросы', unit: 'раз', rows: data.value!.domains },
  { title: 'Частые блокировки', unit: 'раз', rows: data.value!.blocks, red: true },
])
function when(i: number) {
  const back = data.value!.q.length - 1 - i
  if (data.value!.units === 'days') return new Date(Date.now() - back * 864e5).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' })
  return hourRangeRu((new Date().getHours() - back + 24 * 4) % 24)
}
</script>

<template>
  <div>
    <div v-if="error && !data" class="card">
      <h3>{{ errText(error, 'AdGuard недоступен') }}</h3>
    </div>
    <template v-else-if="data">
      <div data-testid="dns-kpi" class="grid gap-3 md:grid-cols-4">
        <div v-for="c in cards" :key="c.title" class="card" :class="{ lit: c.lit }">
          <h3>{{ c.title }}</h3>
          <div class="my-3 flex items-end justify-between gap-2">
            <span class="big">{{ c.big }}</span><span v-if="c.tag" class="tag" :class="{ badt: c.red }">{{ c.tag }}</span>
          </div>
          <LineChart :data="c.line.length > 1 ? c.line : [0, 0]" :h="40" :col="c.red ? 'var(--red-t)' : undefined" />
        </div>
      </div>
      <div class="mt-3 grid gap-3 md:grid-cols-[2fr_1fr]">
        <div class="card">
          <div class="fhead">
            <h3>{{ data.units === 'hours' ? 'Запросы за сутки' : `Запросы за ${data.q.length} дн.` }}</h3>
            <span class="text-xs text-(--t3)"><span class="dot bg-(--acc)" /> Все запросы · <span class="dot bg-(--red)" /> Заблокированные</span>
          </div>
          <LineChart :data="data.q" :extra="data.b" :h="200" :tip="i => `${ru(data!.q[i] ?? 0)} запросов · ${ru(data!.b[i] ?? 0)} заблок. · ${when(i)}`" />
        </div>
        <div class="card">
          <div class="fhead">
            <h3>Серверы выше</h3><span class="tag">{{ data.mode }}</span>
          </div>
          <div v-for="u in data.upstreams" :key="u.url" data-testid="dns-upstream" class="mt-3 text-sm">
            <div class="flex gap-2">
              <b class="mono min-w-0 flex-1 truncate font-normal">{{ u.url }}</b><span>{{ u.pct }}%</span><em class="not-italic text-(--t2)">{{ ms(u.ms) }} мс</em>
            </div>
            <div class="mt-1 h-1 rounded bg-(--line)">
              <i class="block h-full rounded bg-(--acc)" :style="{ width: `${u.pct / upMax * 92}%` }" />
            </div>
          </div>
        </div>
      </div>
      <div class="mt-3 grid gap-3 md:grid-cols-3">
        <div v-for="l in lists" :key="l.title" class="card">
          <div class="fhead">
            <h3>{{ l.title }}</h3><span class="tag">{{ l.unit }}</span>
          </div>
          <div v-for="[name, c] in l.rows" :key="name" class="mt-2 text-sm">
            <div class="flex gap-2">
              <span class="min-w-0 flex-1 truncate">{{ name }}</span><b class="font-medium">{{ ru(c) }}</b>
            </div>
            <div class="mt-1 h-1 rounded bg-(--line)">
              <i class="block h-full rounded" :class="l.red ? 'bg-(--red)' : 'bg-(--g2)'" :style="{ width: `${c / (l.rows[0]?.[1] || 1) * 100}%` }" />
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
