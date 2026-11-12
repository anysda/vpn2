<script setup lang="ts">
import { DnsClients, DnsFilters, DnsLog, DnsRewrites, DnsRules, DnsServices, DnsSettings, DnsSummary } from '#components'

useHead({ title: 'DNS — anysda-vpn2' })
definePageMeta({ title: 'DNS' })

const SUBS = [
  { value: 'sum', label: 'Сводка', view: DnsSummary },
  { value: 'log', label: 'Журнал', view: DnsLog },
  { value: 'flt', label: 'Фильтры', view: DnsFilters },
  { value: 'rules', label: 'Свои правила', view: DnsRules },
  { value: 'rw', label: 'Перезаписи', view: DnsRewrites },
  { value: 'svc', label: 'Сервисы', view: DnsServices },
  { value: 'cl', label: 'Клиенты', view: DnsClients },
  { value: 'set', label: 'Настройки DNS', view: DnsSettings },
] as const
const sub = ref<typeof SUBS[number]['value']>('sum')
// Подраздел монтируется при первом заходе и дальше живёт со своим состоянием.
const seen = reactive(new Set([sub.value]))
watch(sub, v => seen.add(v))
</script>

<template>
  <main class="tab on dns">
    <div class="head">
      <SegTabs v-model="sub" :options="SUBS" class="max-w-full" />
    </div>
    <section v-for="s in SUBS.filter(x => seen.has(x.value))" v-show="sub === s.value" :key="s.value" data-testid="dns-panel">
      <component :is="s.view" />
    </section>
  </main>
</template>
