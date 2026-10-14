<script setup lang="ts">
// Клиенты панели глазами AdGuard: адреса в туннеле, свои настройки, запросы.
import { pluralRu } from '#shared/plural'

interface Row {
  name: string
  tg: string
  ips: string[]
  filters: 'common' | 'off'
  safesearch: 'global' | 'on' | 'off'
  services: string | null
  queries: number
}

const { data, error } = useFetch<Row[]>('/api/dns/clients', { server: false, lazy: true })

const errText = (e: unknown) => {
  const x = e as { data?: { message?: string }, statusMessage?: string, message?: string }
  return x.data?.message ?? x.statusMessage ?? x.message ?? 'Ошибка'
}

const tag = computed(() => {
  const rows = data.value ?? []
  const n = rows.length
  const ips = rows.reduce((a, r) => a + r.ips.length, 0)
  return `${n} ${pluralRu(n, ['клиент', 'клиента', 'клиентов'])} · ${ips} ${pluralRu(ips, ['адрес', 'адреса', 'адресов'])}`
})

const ini = (n: string) => n.split(' ').map(s => s[0]).join('').slice(0, 2)
const ips = (l: string[]) => l.length ? l.slice(0, 2).join(', ') + (l.length > 2 ? ` +${l.length - 2}` : '') : '—'
</script>

<template>
  <div>
    <div v-if="error && !data" class="card">
      <h3>{{ errText(error) }}</h3>
    </div>
    <div class="card fl">
      <div class="fhead">
        <div>
          <h3>Клиенты DNS</h3>
          <p class="sub2">
            Узнаются по адресу в туннеле. Настройки здесь сильнее общих
          </p>
        </div>
        <span v-if="data" class="tag">{{ tag }}</span>
      </div>
      <div class="dct">
        <div class="th">
          <span>Клиент</span><span>Адреса</span><span>Фильтры</span><span>Безопасный поиск</span><span>Сервисы</span><span>Запросов</span>
        </div>
      </div>
      <div class="dct scroll">
        <div v-for="c in data ?? []" :key="c.name">
          <div class="who">
            <span class="av">{{ ini(c.name) }}</span>
            <div><b>{{ c.name }}</b><small>{{ c.tg }}</small></div>
          </div>
          <span class="mono" style="color:var(--t2)">{{ ips(c.ips) }}</span>
          <span><span v-if="c.filters === 'common'" class="tag ok">общие</span><span v-else class="tag">выключены</span></span>
          <span>
            <span v-if="c.safesearch === 'on'" class="tag ok">включён</span>
            <span v-else-if="c.safesearch === 'off'" class="tag">выключен</span>
            <span v-else class="tag">как у всех</span>
          </span>
          <span>{{ c.services ?? 'как у всех' }}</span>
          <span>{{ grp(c.queries) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
