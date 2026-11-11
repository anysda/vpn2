<script setup lang="ts">
import type { Route } from '~/composables/useRoutes'
import type { Cell } from '~/utils/routes-view'

/** Верх раздела: проверка адреса и форма нового правила. */
const props = defineProps<{
  rules: Route[]
  cells: Cell[]
  create: (value: string, outbound: string) => Promise<Route>
}>()
const emit = defineEmits<{
  /** Правило, под которое попал проверенный адрес: доска его подсвечивает. */
  hit: [routeId: number | null]
  added: [id: number]
}>()

const toast = useToast()

const options = computed(() => props.cells.map(c => ({ label: c.name, value: c.outbound })))
const cellName = (outbound: string) => props.cells.find(c => c.outbound === outbound)?.name ?? outbound

// ---------- новое правило ----------
const newValue = ref('')
const newOutbound = ref('')
const adding = ref(false)

watch(options, (list) => {
  if (!newOutbound.value && list.length > 0) {
    newOutbound.value = list.find(o => o.value === 'direct-ru')?.value ?? list[0]!.value
  }
}, { immediate: true })

async function add() {
  // Второй Enter, пока первый запрос в полёте, слал дубль и ловил 409 поверх
  // уже добавленного правила.
  if (adding.value || !newValue.value.trim() || !newOutbound.value) return
  adding.value = true
  try {
    const r = await props.create(newValue.value.trim(), newOutbound.value)
    emit('added', r.id)
    toast.add({ title: 'Правило добавлено', color: 'success' })
    newValue.value = ''
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка', color: 'error' })
  }
  finally {
    adding.value = false
  }
}

// ---------- проверка адреса ----------
interface CheckResult {
  query: string
  reason: 'manual' | 'client' | 'ru' | 'youtube' | 'reject' | 'rule' | 'default'
  rule: string
  routeId: number | null
  outbound: string
  delay: number | null
}
const WHY: Record<CheckResult['reason'], string> = {
  manual: 'своё правило',
  client: 'выбран у клиента',
  ru: 'российский адрес',
  youtube: 'YouTube через zapret',
  reject: 'заблокировано',
  rule: 'правило роутера',
  default: 'ни одно правило не подошло',
}

// Пустое поле: под ним разбор примера серым, пока не ввели своё.
const EXAMPLE = 'chatgpt.com'
const query = ref('')
// Серый, пока на экране ответ для примера: при наборе пример не вспыхивает,
// а сменяется ответом для введённого адреса.
const isExample = ref(true)
const checked = ref<CheckResult | null>(null)
const checkError = ref('')
let checkSeq = 0
let checkTimer: ReturnType<typeof setTimeout> | null = null

const checkRule = computed(() => {
  const r = checked.value
  if (!r) return ''
  if (r.reason === 'default') return 'по умолчанию'
  if (r.reason === 'client') return 'предпочитаемый экзит'
  return r.reason === 'manual' ? `правило ${r.rule}` : r.rule
})
const checkOut = computed(() => {
  const ob = checked.value?.outbound ?? ''
  if (ob === 'youtube-ru') return `${countryName('ru')} · zapret`
  return cellName(ob)
})

async function runCheck() {
  const seq = ++checkSeq
  const q = query.value.trim() || EXAMPLE
  try {
    const r = await $fetch<CheckResult>('/api/routes/check', { query: { q } })
    if (seq !== checkSeq) return
    checked.value = r
    isExample.value = !query.value.trim()
    checkError.value = ''
  }
  catch (e) {
    if (seq !== checkSeq) return
    const err = e as { data?: { message?: string }, statusMessage?: string }
    checked.value = null
    checkError.value = err.data?.message ?? err.statusMessage ?? 'Ошибка'
  }
}
// Ответ сервера зависит от правил: после переноса, добавления и удаления
// проверка повторяется.
function scheduleCheck(delay = 250) {
  if (checkTimer) clearTimeout(checkTimer)
  checkTimer = setTimeout(() => void runCheck(), delay)
}
watch(query, () => scheduleCheck())
watch(() => props.rules.map(r => `${r.id}:${r.outbound}`).join(), () => scheduleCheck(0))
watch(checked, r => emit('hit', r?.routeId ?? null))
onMounted(() => scheduleCheck(0))
onBeforeUnmount(() => {
  if (checkTimer) clearTimeout(checkTimer)
})
</script>

<template>
  <section class="rtop">
    <div class="card chk">
      <h3>Проверить адрес</h3>
      <label class="inp big2"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" /></svg><input
        v-model="query" :placeholder="EXAMPLE" spellcheck="false" aria-label="Проверить адрес"></label>
      <div :class="['chain', { demo: isExample }]" data-testid="route-check">
        <template v-if="checked">
          <div class="ch" style="--bar:var(--acc)">
            {{ checked.query }}
          </div><span class="ar2" /><div class="ch" style="--bar:var(--g2)">
            {{ checkRule }}
          </div><span class="ar2" /><div :class="['ch', { bad: checked.delay === null }]" style="--bar:var(--acc)">
            {{ checkOut }}<b>{{ checked.delay ?? '—' }} мс</b>
          </div><span class="why">{{ WHY[checked.reason] }}</span>
        </template>
        <span v-else-if="checkError" class="why">{{ checkError }}</span>
      </div>
    </div>
    <form class="card addr" @submit.prevent="add">
      <h3>Новое правило</h3>
      <label class="inp big2"><input
        v-model="newValue" placeholder="netflix.com / *.openai.com / 8.8.8.8/32" spellcheck="false" aria-label="Новое правило"></label>
      <div class="arow">
        <USelect
          v-model="newOutbound" :items="options" variant="none" trailing-icon="i-lucide-chevron-down" class="sel"
          :ui="{ base: 'sel-b', trailing: 'pe-[13px]', trailingIcon: 'size-[17px] text-(--t2)', value: 'text-(--t1)' }" aria-label="Выход" />
        <button type="submit" class="pill w" :disabled="!newValue.trim() || !newOutbound">
          Добавить
        </button>
      </div>
    </form>
  </section>
</template>

<style scoped>
.card h3{font-size:14px;font-weight:500;color:var(--t2)}
.rtop{display:grid;grid-template-columns:7fr 5fr;gap:12px;margin-bottom:12px}
.chain{display:flex;align-items:center;gap:0;margin-top:16px;min-height:40px;flex-wrap:wrap;row-gap:8px;transition:opacity .2s ease}
.chain.demo{opacity:.45}
.ch{position:relative;display:flex;align-items:center;gap:10px;height:36px;padding:0 12px 0 14px;border-radius:9px;background:var(--chip);border:1px solid var(--line);font-size:12.5px;color:var(--t2);cursor:default;transition:opacity .25s var(--ease),border-color .2s,background .2s;white-space:nowrap}
.ch::before{content:"";position:absolute;left:6px;top:9px;bottom:9px;width:2px;border-radius:2px;background:var(--bar,rgba(var(--fg),.25))}
.ch b{margin-left:auto;color:var(--t1);font-weight:600;font-size:12.5px}
.ch.bad{border-color:rgba(var(--red-rgb),.35);background:linear-gradient(90deg,rgba(var(--red-rgb),.12),var(--chip) 70%);color:var(--red-t2)}
.chain .ar2{width:34px;height:1px;background:rgba(var(--fg),.25);position:relative}
.chain .ar2::after{content:"";position:absolute;right:0;top:-3px;border:3.5px solid transparent;border-left:5px solid rgba(var(--fg),.4);border-right:0}
.chain .why{margin-left:14px;color:var(--t3);font-size:12px}
.inp{display:flex;align-items:center;gap:8px;height:36px;padding:0 14px;border-radius:999px;background:rgba(var(--fg),.04);border:1px solid var(--line);color:var(--t3);transition:border-color .2s}
.inp:focus-within{border-color:rgba(var(--fg),.3)}
.inp input{background:none;border:0;outline:0;color:var(--t1);font:500 13px var(--font-sans);width:200px}
.inp input::placeholder{color:var(--t3)}
.inp.big2{height:50px;border-radius:14px;padding:0 16px;background:var(--sunk);box-shadow:inset 0 2px 10px rgba(0,0,0,calc(.5*var(--shk)));margin-top:14px}
.inp.big2 input{font-size:16px;width:100%}
.arow{display:flex;gap:8px;margin-top:12px;align-items:center}
.arow .sel{flex:1;min-width:0}
.arow :deep(.sel-b){height:40px;width:100%;padding:0 38px 0 16px;border-radius:999px;border:1px solid var(--line);background:rgba(var(--fg),.04);color:var(--t1);font:500 13px var(--font-sans);cursor:pointer;outline:0}
.arow .pill{height:40px}
/* Телефон: карточки друг под другом. */
@media (max-width:720px){
  .rtop{grid-template-columns:minmax(0,1fr);margin-bottom:0}
  .rtop .card{padding:16px}
  .inp.big2{height:44px;padding:0 14px;margin-top:12px}
  .chain{margin-top:10px;min-height:0;gap:6px;font-size:12px;color:var(--t2)}
  .ch{height:28px;padding:0 10px;gap:8px;font-size:12px}
  .ch::before{display:none}
  .ch b{font-size:12px}
  .chain .ar2{width:12px}
  .chain .why{flex-basis:100%;margin-left:0;font-size:11.5px}
  .arow{margin-top:10px}
}
</style>
