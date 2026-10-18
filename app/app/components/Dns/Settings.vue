<script setup lang="ts">
// Настройки DNS AdGuard: серверы выше, защита, кэш, журнал. Каждое изменение —
// одно поле; ответ сервера — то, что AdGuard показывает после изменения.
import { pluralRu } from '#shared/plural'

interface View {
  upstreams: string
  mode: 'load_balance' | 'parallel' | 'fastest_addr'
  bootstrap: string
  safebrowsing: boolean
  parental: boolean
  safesearch: boolean
  dnssec: boolean
  noAaaa: boolean
  cache: string
  optimistic: boolean
  ratelimit: string
  block: string
  logHours: number
  statsHours: number
  anonymize: boolean
}
type Tog = 'safebrowsing' | 'parental' | 'safesearch' | 'dnssec' | 'noAaaa' | 'optimistic' | 'anonymize'
type Edit = 'bootstrap' | 'ratelimit' | 'block'

const MODES = [
  { value: 'load_balance', label: 'Балансировка' },
  { value: 'parallel', label: 'Параллельно' },
  { value: 'fastest_addr', label: 'Самый быстрый' },
] as const
const LOG = [{ value: '24', label: '1 день' }, { value: '168', label: '7 дней' }, { value: '2160', label: '90 дней' }] as const
const STATS = [{ value: '24', label: '24 часа' }, { value: '168', label: '7 дней' }, { value: '720', label: '30 дней' }] as const

const PROTECT: { k: Tog, name: string, note: string }[] = [
  { k: 'safebrowsing', name: 'Безопасная навигация', note: 'фишинг и вредоносные сайты' },
  { k: 'parental', name: 'Родительский контроль', note: 'сайты для взрослых' },
  { k: 'safesearch', name: 'Безопасный поиск', note: 'Google, Яндекс, YouTube' },
  { k: 'dnssec', name: 'DNSSEC', note: 'проверять подписи ответов' },
  { k: 'noAaaa', name: 'Не отвечать на AAAA', note: 'отключить IPv6-адреса' },
]

const toast = useToast()
const { data, error, status, refresh } = useFetch<View>('/api/dns/settings', { server: false, lazy: true })
const latest = latestWins<View>(v => (data.value = v), () => $fetch<View>('/api/dns/settings'))
// Пока серверы сохраняются, поле только для чтения: ответ PATCH иначе затёр
// бы допечатанное, а тост «сохранено» говорил бы не о том, что видно.
const ups = ref('')
watch(() => data.value?.upstreams, (v) => {
  if (v !== undefined) ups.value = v
}, { immediate: true })
const busy = ref(new Set<string>())

const errText = (e: unknown) => {
  const x = e as { data?: { message?: string }, statusMessage?: string, message?: string }
  return x.data?.message ?? x.statusMessage ?? x.message ?? 'Ошибка'
}

async function patch(body: Partial<Record<keyof View, unknown>>, done?: string): Promise<boolean> {
  const k = Object.keys(body)[0]!
  busy.value.add(k)
  try {
    await latest(() => $fetch<View>('/api/dns/settings', { method: 'PATCH', body }))
    if (done) toast.add({ title: done })
    return true
  }
  catch (e) {
    toast.add({ title: errText(e), color: 'error' })
    return false
  }
  finally {
    busy.value.delete(k)
  }
}

function tog(k: Tog) {
  if (!data.value) return
  const want = !data.value[k]
  data.value[k] = want
  void patch({ [k]: want })
}

function saveUps() {
  const norm = (s: string) => s.split('\n').map(x => x.trim()).filter(Boolean).join('\n')
  if (!data.value || norm(ups.value) === data.value.upstreams) return
  void patch({ upstreams: ups.value }, 'Серверы выше: сохранено')
}

const mode = computed(() => data.value?.mode ?? 'load_balance')
const logH = computed(() => String(data.value?.logHours ?? '') as '24')
const statsH = computed(() => String(data.value?.statsHours ?? '') as '24')

const checking = ref(false)
async function check() {
  checking.value = true
  try {
    const r = await $fetch<{ total: number, failed: string[] }>('/api/dns/settings/upstreams-check', {
      method: 'POST', body: { upstreams: ups.value, bootstrap: data.value?.bootstrap ?? '' },
    })
    if (r.failed.length) toast.add({ title: `Не отвечают: ${r.failed.join(', ')}`, color: 'error' })
    else toast.add({ title: r.total === 1 ? 'Сервер отвечает' : `Все ${r.total} ${pluralRu(r.total, ['сервер', 'сервера', 'серверов'])} отвечают` })
  }
  catch (e) {
    toast.add({ title: errText(e), color: 'error' })
  }
  finally {
    checking.value = false
  }
}

// «Изменить»: окно с одним полем, заголовок и подпись — название строки.
const editOpen = ref(false)
const edit = ref<{ k: Edit, name: string, mono: boolean } | null>(null)
const editVal = ref('')
function openEdit(k: Edit, name: string, mono = false) {
  edit.value = { k, name, mono }
  editVal.value = data.value?.[k] ?? ''
  editOpen.value = true
}
async function saveEdit() {
  const e = edit.value
  if (!e || busy.value.has(e.k)) return
  if (await patch({ [e.k]: editVal.value }, `${e.name}: сохранено`)) editOpen.value = false
}

// Очистка кэша и сброс журнала или статистики — через подтверждение.
type Ask = 'cache' | 'log' | 'stats'
const ASK: Record<Ask, { title: string, text: string, btn: string, done: string }> = {
  cache: { title: 'Очистить кэш DNS?', text: 'Сохранённые ответы удалятся, первые запросы после этого пойдут на серверы выше и будут чуть медленнее.', btn: 'Очистить', done: 'Кэш DNS очищен' },
  log: { title: 'Сбросить журнал?', text: 'Все записи журнала запросов удалятся. Восстановить их будет нельзя.', btn: 'Сбросить', done: 'Журнал очищен' },
  stats: { title: 'Сбросить статистику?', text: 'Все записи статистики удалятся. Восстановить их будет нельзя.', btn: 'Сбросить', done: 'Статистика сброшена' },
}
const askOpen = ref(false)
const ask = ref<Ask>('cache')
function openAsk(a: Ask) {
  ask.value = a
  askOpen.value = true
}
async function confirmAsk() {
  const a = ask.value
  askOpen.value = false
  try {
    if (a === 'cache') await $fetch('/api/dns/settings/cache-clear', { method: 'POST' })
    else await $fetch('/api/dns/settings/reset', { method: 'POST', body: { what: a } })
    toast.add({ title: ASK[a].done })
  }
  catch (e) {
    toast.add({ title: errText(e), color: 'error' })
  }
}
</script>

<template>
  <div>
    <DnsLoadError v-if="error && !data" :message="errText(error)" :pending="status === 'pending'" @retry="refresh()" />
    <div v-if="data" class="sgrid">
      <div class="card sc">
        <div class="sh">
          <h3>Серверы выше</h3>
          <button type="button" class="pill" :disabled="checking" @click="check">
            Проверить
          </button>
        </div>
        <textarea v-model="ups" class="mono" spellcheck="false" style="min-height:104px;flex:none" aria-label="Серверы выше" :readonly="busy.has('upstreams')" @blur="saveUps" />
        <div class="arow">
          <DnsSeg :model-value="mode" small :options="MODES" @pick="v => patch({ mode: v })" />
        </div>
        <div class="fld">
          <span>Bootstrap</span><code class="mono"><WholeWords :text="data.bootstrap" /></code>
          <button type="button" class="pill" @click="openEdit('bootstrap', 'Bootstrap', true)">
            Изменить
          </button>
        </div>
      </div>
      <div class="card sc">
        <div class="sh">
          <h3>Защита</h3>
        </div>
        <div v-for="p in PROTECT" :key="p.k" class="fld">
          <span>{{ p.name }}</span><code>{{ p.note }}</code>
          <button type="button" class="tog" :class="{ on: data[p.k] }" role="switch" :aria-checked="data[p.k]" :aria-label="p.name" :disabled="busy.has(p.k)" @click="tog(p.k)">
            <i />
          </button>
        </div>
      </div>
      <div class="card sc">
        <div class="sh">
          <h3>Кэш и ограничения</h3>
        </div>
        <div class="fld">
          <span>Размер кэша</span><code><WholeWords :text="data.cache" /></code>
          <button type="button" class="pill" @click="openAsk('cache')">
            Очистить
          </button>
        </div>
        <div class="fld">
          <span>Оптимистичный кэш</span><code>отвечать устаревшим, пока обновляется</code>
          <button type="button" class="tog" :class="{ on: data.optimistic }" role="switch" :aria-checked="data.optimistic" aria-label="Оптимистичный кэш" :disabled="busy.has('optimistic')" @click="tog('optimistic')">
            <i />
          </button>
        </div>
        <div class="fld">
          <span>Лимит запросов</span><code><WholeWords :text="data.ratelimit" /></code>
          <button type="button" class="pill" @click="openEdit('ratelimit', 'Лимит запросов')">
            Изменить
          </button>
        </div>
        <div class="fld">
          <span>Ответ на блокировку</span><code><WholeWords :text="data.block" /></code>
          <button type="button" class="pill" @click="openEdit('block', 'Ответ на блокировку')">
            Изменить
          </button>
        </div>
      </div>
      <div class="card sc">
        <div class="sh">
          <h3>Журнал и статистика</h3>
        </div>
        <div class="fld">
          <span>Хранить журнал</span>
          <DnsSeg :model-value="logH" small :options="LOG" @pick="v => patch({ logHours: Number(v) })" />
        </div>
        <div class="fld">
          <span>Хранить статистику</span>
          <DnsSeg :model-value="statsH" small :options="STATS" @pick="v => patch({ statsHours: Number(v) })" />
        </div>
        <div class="fld">
          <span>Скрывать адреса клиентов</span><code>в журнале последний байт обнуляется</code>
          <button type="button" class="tog" :class="{ on: data.anonymize }" role="switch" :aria-checked="data.anonymize" aria-label="Скрывать адреса клиентов" :disabled="busy.has('anonymize')" @click="tog('anonymize')">
            <i />
          </button>
        </div>
        <div class="fld">
          <span>Сбросить</span><code />
          <button type="button" class="pill" @click="openAsk('log')">
            Журнал
          </button>
          <button type="button" class="pill" @click="openAsk('stats')">
            Статистику
          </button>
        </div>
      </div>
    </div>

    <DnsModal v-model:open="editOpen" :title="edit?.name ?? ''" close-btn>
      <label class="fl2"><span>{{ edit?.name }}</span><input v-model="editVal" class="in" :class="{ mono: edit?.mono }" spellcheck="false" @keydown.enter="saveEdit"></label>
      <template #foot="{ close }">
        <button type="button" class="pill" @click="close">
          Отмена
        </button>
        <button type="button" class="pill w" :disabled="!edit || busy.has(edit.k)" @click="saveEdit">
          Сохранить
        </button>
      </template>
    </DnsModal>

    <DnsModal v-model:open="askOpen" :title="ASK[ask].title">
      <p class="mt">
        {{ ASK[ask].text }}
      </p>
      <template #foot="{ close }">
        <button type="button" class="pill" @click="close">
          Отмена
        </button>
        <button type="button" class="pill" :class="ask === 'cache' ? 'w' : 'redw'" @click="confirmAsk">
          {{ ASK[ask].btn }}
        </button>
      </template>
    </DnsModal>
  </div>
</template>
