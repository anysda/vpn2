<script setup lang="ts">
// Свои правила AdGuard и проверка фильтрации. Текст с ошибкой не уходит в
// AdGuard и остаётся в поле; проверка спрашивает AdGuard о применённых правилах.
// Поле — весь набор правил целиком: до ответа GET оно закрыто (правка вслепую
// заменила бы правила, которых человек не видел), а пока идёт «Применить» —
// только для чтения, чтобы ответ PUT не затёр допечатанное. Упавший GET
// показывает причину и «Повторить загрузку».
interface Verdict { kind: 'blk' | 'ok' | 'rw', label: string, why: [string, string, string] }

const { data, load, toast, busy, act } = useDnsSection<{ text: string }>('/api/dns/rules')
const text = ref('')
watch(data, (v) => {
  if (v) text.value = v.text
}, { immediate: true })
const q = ref('')
const res = ref<Verdict | null>(null)
const fres = ref<HTMLElement>()
let seq = 0
// AdGuard пересобирает фильтры в фоне и ещё с полсекунды отвечает по старым
// правилам: 2 с после «Применить» проверку повторяем, в том числе для домена,
// набранного уже после применения.
let settleUntil = 0

async function check() {
  const name = q.value.trim().toLowerCase().replace(/^https?:\/\//, '').split('/')[0] ?? ''
  const my = ++seq
  if (!name) {
    res.value = null
    return
  }
  try {
    const v = await $fetch<Verdict>('/api/dns/check', { query: { name } })
    if (my !== seq) return
    const changed = v.label !== res.value?.label
    res.value = v
    if (Date.now() < settleUntil) setTimeout(() => my === seq && void check(), 250)
    if (changed) {
      await nextTick()
      rise(fres.value?.firstElementChild, { duration: 220 }, 4)
    }
  }
  catch (e) {
    if (my !== seq) return
    res.value = null
    if ((e as { statusCode?: number }).statusCode !== 400) toast.add({ title: errText(e), color: 'error' })
  }
}

watch(q, debounce(() => void check(), 250))

const apply = () => act('save', async () => {
  text.value = (await $fetch<{ text: string }>('/api/dns/rules', { method: 'PUT', body: { text: text.value } })).text
  settleUntil = Date.now() + 2000
  void check()
}, 'Свои правила применены')
</script>

<template>
  <DnsLoadError :of="load" />
  <div class="rgrid">
    <div class="card cr">
      <DnsHead title="Свои правила" sub="Одно правило на строку. Срабатывают раньше списков">
        <button type="button" class="pill w" :disabled="!data || busy.has('save')" @click="apply">
          Применить
        </button>
      </DnsHead>
      <textarea v-model="text" spellcheck="false" aria-label="Свои правила" :disabled="!data" :readonly="busy.has('save')" />
    </div>
    <div class="rside">
      <div class="card chk2">
        <h3>Проверка фильтрации</h3>
        <label class="inp big2">
          <svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" /></svg>
          <input v-model="q" spellcheck="false" aria-label="Домен для проверки">
        </label>
        <div ref="fres" class="fres">
          <template v-if="res">
            <div class="fv" :class="res.kind">
              <span class="big">{{ res.label }}</span>
            </div>
            <p>{{ res.why[0] }}<code v-if="res.why[1]">{{ res.why[1] }}</code>{{ res.why[2] }}</p>
          </template>
        </div>
      </div>
      <div class="card syn">
        <h3>Как писать</h3>
        <dl>
          <dt>||site.com^</dt><dd>заблокировать вместе с поддоменами</dd>
          <dt>@@||site.com^</dt><dd>разблокировать, даже если есть в списках</dd>
          <dt>$client='Имя'</dt><dd>только для одного клиента</dd>
          <dt>$important</dt><dd>сильнее разблокировки</dd>
          <dt>! текст</dt><dd>комментарий</dd>
        </dl>
      </div>
    </div>
  </div>
</template>
