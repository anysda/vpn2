<script setup lang="ts">
// Свои правила AdGuard и проверка фильтрации. Текст с ошибкой не уходит в
// AdGuard и остаётся в поле; проверка спрашивает AdGuard о применённых правилах.
// Поле — весь набор правил целиком: до ответа GET оно закрыто (правка вслепую
// заменила бы правила, которых человек не видел), а пока идёт «Применить» —
// только для чтения, чтобы ответ PUT не затёр допечатанное.
interface Verdict { kind: 'blk' | 'ok' | 'rw', label: string, why: [string, string, string] }

const toast = useToast()
const text = ref('')
const loaded = ref(false)
const saving = ref(false)
const q = ref('')
const res = ref<Verdict | null>(null)
const fres = ref<HTMLElement>()
let seq = 0

const errText = (e: unknown) => {
  const x = e as { data?: { message?: string }, statusMessage?: string, message?: string }
  return x.data?.message ?? x.statusMessage ?? x.message ?? 'Ошибка'
}

onMounted(async () => {
  try {
    text.value = (await $fetch<{ text: string }>('/api/dns/rules')).text
    loaded.value = true
  }
  catch (e) {
    toast.add({ title: errText(e), color: 'error' })
  }
})

async function check(): Promise<boolean> {
  const name = q.value.trim().toLowerCase().replace(/^https?:\/\//, '').split('/')[0] ?? ''
  const my = ++seq
  if (!name) {
    res.value = null
    return false
  }
  try {
    const v = await $fetch<Verdict>('/api/dns/check', { query: { name } })
    if (my !== seq) return false
    const changed = v.label !== res.value?.label
    const same = JSON.stringify(v) === JSON.stringify(res.value)
    res.value = v
    if (changed) {
      await nextTick()
      anim(fres.value?.firstElementChild, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: 220 })
    }
    return !same
  }
  catch (e) {
    if (my === seq) res.value = null
    if (my === seq && (e as { statusCode?: number }).statusCode !== 400) toast.add({ title: errText(e), color: 'error' })
    return false
  }
}

// AdGuard пересобирает фильтры в фоне и ещё с полсекунды отвечает по старым
// правилам: после «Применить» спрашиваем, пока ответ не сменится (до 2 с).
async function recheck() {
  for (let i = 0; i < 8; i++) {
    await new Promise(r => setTimeout(r, 250))
    if (await check()) return
  }
}

let typing: ReturnType<typeof setTimeout> | undefined
watch(q, () => {
  clearTimeout(typing)
  typing = setTimeout(() => void check(), 250)
})

async function apply() {
  saving.value = true
  try {
    text.value = (await $fetch<{ text: string }>('/api/dns/rules', { method: 'PUT', body: { text: text.value } })).text
    toast.add({ title: 'Свои правила применены' })
    void recheck()
  }
  catch (e) {
    toast.add({ title: errText(e), color: 'error' })
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="rgrid">
    <div class="card cr">
      <div class="fhead">
        <div>
          <h3>Свои правила</h3>
          <p class="sub2">
            Одно правило на строку. Срабатывают раньше списков
          </p>
        </div>
        <button type="button" class="pill w" :disabled="!loaded || saving" @click="apply">
          Применить
        </button>
      </div>
      <textarea v-model="text" spellcheck="false" aria-label="Свои правила" :disabled="!loaded" :readonly="saving" />
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
