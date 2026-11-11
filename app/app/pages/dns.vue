<script setup lang="ts">
import '~/assets/css/dns.css'
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
type Sub = typeof SUBS[number]['value']

const sub = ref<Sub>('sum')
// Подраздел монтируется при первом заходе и дальше живёт, чтобы не терять состояние.
const seen = reactive(new Set<Sub>(['sum']))

async function pick(v: Sub, dir: 'fwd' | 'back') {
  await subTransition(dir, async () => {
    seen.add(v)
    sub.value = v
    await nextTick()
    cascade(document.querySelector<HTMLElement>('.dns .dpan.on'))
  })
}
</script>

<template>
  <main class="tab on dns">
    <div class="head">
      <DnsSeg v-model="sub" class="dseg" :options="SUBS" @pick="pick" />
    </div>
    <section v-for="s in SUBS.filter(x => seen.has(x.value))" :key="s.value" class="dpan" :class="{ on: sub === s.value }">
      <component :is="s.view" />
    </section>
  </main>
</template>
