<script setup lang="ts">
import { plural } from '#shared/plural'

type View = Api<'/api/dns/filters'>

const { data, load, busy, change } = useDns('/api/dns/filters')

function every(h: number) {
  if (!h) return 'не обновляются'
  if (h < 24) return h === 1 ? 'обновляются раз в час' : `обновляются раз в ${h} ${plural(h, 'hour')}`
  const d = Math.round(h / 24)
  return d === 1 ? 'обновляются раз в сутки' : d === 7 ? 'обновляются раз в неделю' : `обновляются раз в ${d} ${plural(d, 'day')}`
}
const sections = computed(() => {
  const v = data.value
  if (!v) return []
  const on = v.black.filter(l => l.enabled)
  const rules = on.reduce((a, l) => a + l.rules, 0)
  return [
    { white: false, title: 'Чёрные списки', sub: `Включено ${on.length} из ${v.black.length} · ${ru(rules)} ${plural(rules, 'rule')} · ${every(v.interval)}`, rows: v.black },
    { white: true, title: 'Белые списки', sub: 'Адреса отсюда не блокируются никогда', rows: v.white },
  ]
})
const updated = (l: View['black'][number]) => !l.enabled ? 'выключен' : l.updated ? todayYesterdayHmLocal(l.updated) : '—'
const FIELDS = [{ k: 'name', label: 'Название', placeholder: 'напр. HaGeZi Light' }, { k: 'url', label: 'Адрес списка', placeholder: 'https://…', mono: true }]
const check = () => change<View & { updated: number }>('check', 'POST', undefined,
  r => r.updated ? `Списки обновлены: ${r.updated} ${plural(r.updated, 'list')}` : 'Списки обновлены, новых правил нет', '/refresh')
</script>

<template>
  <div>
    <DnsLoadError :of="load" />
    <DnsList
      v-for="s in sections" :key="s.title" :title="s.title" :sub="s.sub" :rows="s.rows" add-label="Добавить список" :fields="FIELDS"
      :cols="s.white ? [] : ['', 'Список', 'Правил', 'Обновлён', '']" grid="grid-cols-[42px_minmax(0,1fr)_90px_130px_30px] max-md:grid-cols-[42px_minmax(0,1fr)_30px]"
      :row-key="l => `${s.white}|${l.url}`" :ask="l => ({ title: 'Удалить список?', text: `Удалить список «${l.name}»? Его правила перестанут применяться.` })"
      :toggle="l => change(`${s.white}|${l.url}`, 'PATCH', { url: l.url, white: s.white, enabled: l.enabled })"
      :remove="l => change('del', 'DELETE', { url: l.url, white: s.white }, `Список «${l.name}» удалён`)"
      :add="v => change('add', 'POST', { name: v.name!.trim(), url: v.url!.trim(), white: s.white }, `Список «${v.name!.trim()}» добавлен`)">
      <template v-if="!s.white" #actions>
        <button type="button" class="pill" :disabled="busy.has('check')" @click="check">
          Проверить обновления
        </button>
      </template>
      <template #default="{ row: l }">
        <div class="min-w-0">
          <b class="block truncate font-medium">{{ l.name }}</b>
          <small class="mono block truncate text-xs text-(--t3)">{{ l.url.replace(/^https?:\/\//, '') }}</small>
        </div>
        <span class="max-md:hidden">{{ l.enabled && l.rules ? ru(l.rules) : '—' }}</span>
        <span class="text-(--t2) max-md:hidden">{{ updated(l) }}</span>
      </template>
    </DnsList>
  </div>
</template>
