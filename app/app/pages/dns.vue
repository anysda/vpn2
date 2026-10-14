<script setup lang="ts">
import '~/assets/css/dns.css'

useHead({ title: 'DNS — anysda-vpn2' })

const SUBS = [
  { value: 'sum', label: 'Сводка' },
  { value: 'log', label: 'Журнал' },
  { value: 'flt', label: 'Фильтры' },
  { value: 'rules', label: 'Свои правила' },
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
    <section v-if="seen.has('sum')" class="dpan" :class="{ on: sub === 'sum' }">
      <DnsSummary />
    </section>
    <section v-if="seen.has('log')" class="dpan" :class="{ on: sub === 'log' }">
      <DnsLog />
    </section>
    <section v-if="seen.has('flt')" class="dpan" :class="{ on: sub === 'flt' }">
      <DnsFilters />
    </section>
    <section v-if="seen.has('rules')" class="dpan" :class="{ on: sub === 'rules' }">
      <DnsRules />
    </section>
  </main>
</template>
