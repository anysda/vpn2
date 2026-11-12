<script setup lang="ts">
// Поле - весь набор своих правил: до ответа GET закрыто, пока идёт «Применить» - только для чтения.
type Verdict = Api<'/api/dns/check'>

const { data, load, toast, busy, act } = useDns('/api/dns/rules')
const text = ref('')
watch(data, v => v && (text.value = v.text), { immediate: true })
const q = ref('')
const res = ref<Verdict | null>(null)
let seq = 0, timer: ReturnType<typeof setTimeout> | undefined
// AdGuard пересобирает фильтры в фоне и ещё ~0,5 с отвечает по старым правилам.
let settleUntil = 0

async function check() {
  const name = q.value.trim().toLowerCase().replace(/^https?:\/\//, '').split('/')[0] ?? ''
  const my = ++seq
  if (!name) {
    res.value = null
    return
  }
  try {
    const v = await $fetch('/api/dns/check', { query: { name } })
    if (my !== seq) return
    res.value = v
    if (Date.now() < settleUntil) setTimeout(() => my === seq && void check(), 250)
  }
  catch (e) {
    if (my !== seq) return
    res.value = null
    if ((e as { statusCode?: number }).statusCode !== 400) toast.add({ title: errText(e), color: 'error' })
  }
}
watch(q, () => {
  clearTimeout(timer)
  timer = setTimeout(check, 250)
})

const apply = () => act('save', async () => {
  text.value = (await $fetch<{ text: string }>('/api/dns/rules', { method: 'PUT', body: { text: text.value } })).text
  settleUntil = Date.now() + 2000
  void check()
}, 'Свои правила применены')

const COLOR = { blk: 'text-(--red-t)', ok: 'text-(--green)', rw: 'text-(--t1)' }
const SYNTAX = [
  ['||site.com^', 'заблокировать вместе с поддоменами'],
  ['@@||site.com^', 'разблокировать, даже если есть в списках'],
  ['$client=\'Имя\'', 'только для одного клиента'],
  ['$important', 'сильнее разблокировки'],
  ['! текст', 'комментарий'],
]
</script>

<template>
  <DnsLoadError :of="load" />
  <div class="grid gap-3 md:grid-cols-[7fr_5fr]">
    <div class="card flex flex-col">
      <DnsHead title="Свои правила" sub="Одно правило на строку. Срабатывают раньше списков">
        <button type="button" class="pill w" :disabled="!data || busy.has('save')" @click="apply">
          Применить
        </button>
      </DnsHead>
      <textarea v-model="text" class="area mono min-h-[420px] flex-1" spellcheck="false" aria-label="Свои правила" :disabled="!data" :readonly="busy.has('save')" />
    </div>
    <div class="flex flex-col gap-3">
      <div class="card">
        <h3>Проверка фильтрации</h3>
        <label class="inp big2">
          <svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" /></svg>
          <input v-model="q" spellcheck="false" aria-label="Домен для проверки">
        </label>
        <div v-if="res" class="mt-4">
          <span class="big" :class="COLOR[res.kind]">{{ res.label }}</span>
          <p class="mt-2 text-sm text-(--t2)">
            {{ res.why[0] }}<code v-if="res.why[1]" class="mono">{{ res.why[1] }}</code>{{ res.why[2] }}
          </p>
        </div>
      </div>
      <div class="card">
        <h3>Как писать</h3>
        <dl class="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
          <template v-for="[k, v] in SYNTAX" :key="k">
            <dt class="mono">
              {{ k }}
            </dt>
            <dd class="text-(--t2)">
              {{ v }}
            </dd>
          </template>
        </dl>
      </div>
    </div>
  </div>
</template>
