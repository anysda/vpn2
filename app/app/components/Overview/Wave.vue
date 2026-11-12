<script setup lang="ts">
// Трафик всех экзитов за сутки по часам или за неделю по суткам МСК и сравнение с тем же окном раньше.

const props = defineProps<{ period: 'day' | 'week' }>()
const { data, refresh } = useFetch('/api/ops/traffic', { query: computed(() => ({ period: props.period })), server: false })
usePoll(refresh, 60_000)

const gb = computed(() => (data.value?.total ?? []).map(b => b / 1e9))
const delta = computed(() => {
  const d = data.value
  if (!d?.prev) return null
  const p = Math.round((d.sum - d.prev) / d.prev * 100)
  return `${p > 0 ? '+' : p < 0 ? '−' : ''}${Math.abs(p)}% ${d.period === 'day' ? 'к вчерашнему' : 'к прошлой неделе'}`
})
function tip(i: number) {
  const d = data.value!, s = d.starts[i]!
  return d.period === 'day'
    ? `${ru(gb.value[i]!, 1)} ГБ за час · ${hourRangeRu(mskHourFromSec(s))}`
    : `${ru(gb.value[i]!, 1)} ГБ за сутки · ${mskWeekdayDateFromSec(s)}`
}
</script>

<template>
  <div data-testid="ov-wave" class="card">
    <h3>Трафик за {{ period === 'day' ? 'сутки' : 'неделю' }}</h3>
    <div class="my-3 flex items-end justify-between gap-2">
      <span class="big">{{ data ? gbDecNumRu(data.sum) : '—' }}<small class="text-lg text-(--t3)"> ГБ</small></span>
      <span v-if="delta" class="tag" :title="period === 'day' ? 'Сравнение с тем же временем вчера' : 'Сравнение с тем же временем неделю назад'">{{ delta }}</span>
    </div>
    <LineChart :data="gb" :h="140" :band="peakBand(gb, 5)" :tip="tip" />
  </div>
</template>
