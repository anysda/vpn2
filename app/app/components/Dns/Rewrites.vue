<script setup lang="ts">
interface Rewrite { domain: string, answer: string, enabled: boolean }

const { data, load, change } = useDns<Rewrite[]>('/api/dns/rewrites')
const key = (r: Rewrite) => `${r.domain}|${r.answer}`
const FIELDS = [
  { k: 'domain', label: 'Домен', placeholder: 'напр. nas.home', mono: true },
  { k: 'answer', label: 'Ответ', placeholder: 'IP или домен', mono: true },
]
const add = (v: Record<string, string>) =>
  change('add', 'POST', { domain: v.domain, answer: v.answer }, `Перезапись «${v.domain!.trim().toLowerCase().replace(/\.$/, '')}» добавлена`)
</script>

<template>
  <div>
    <DnsLoadError :of="load" />
    <DnsList
      title="Перезаписи DNS" sub="Свой ответ на запрос: внутренние адреса, заглушки" :rows="data ?? []" add-label="Добавить перезапись" :fields="FIELDS"
      :cols="['', 'Домен', '', 'Ответ', 'Зачем', '']" grid="grid-cols-[42px_minmax(0,1fr)_20px_minmax(0,1fr)_60px_30px]" :row-key="key"
      :ask="r => ({ title: 'Удалить перезапись?', text: `Удалить перезапись «${r.domain}»? Запрос снова пойдёт на серверы выше.` })"
      :toggle="r => change(key(r), 'PATCH', { domain: r.domain, answer: r.answer, enabled: r.enabled })"
      :remove="r => change('del', 'DELETE', { domain: r.domain, answer: r.answer }, `Перезапись «${r.domain}» удалена`)" :add="add">
      <template #default="{ row: r }">
        <span class="mono truncate">{{ r.domain }}</span>
        <span class="text-(--t3)">→</span>
        <span class="mono truncate">{{ r.answer }}</span>
        <span class="text-(--t3)">—</span>
      </template>
    </DnsList>
  </div>
</template>
