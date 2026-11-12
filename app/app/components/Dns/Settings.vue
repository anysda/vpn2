<script setup lang="ts">
import { plural } from '#shared/plural'

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

const MODES = [{ value: 'load_balance', label: 'Балансировка' }, { value: 'parallel', label: 'Параллельно' }, { value: 'fastest_addr', label: 'Самый быстрый' }] as const
const LOG = [{ value: '24', label: '1 день' }, { value: '168', label: '7 дней' }, { value: '2160', label: '90 дней' }]
const STATS = [{ value: '24', label: '24 часа' }, { value: '168', label: '7 дней' }, { value: '720', label: '30 дней' }]
const PROTECT: { k: Tog, name: string, note: string }[] = [
  { k: 'safebrowsing', name: 'Безопасная навигация', note: 'фишинг и вредоносные сайты' },
  { k: 'parental', name: 'Родительский контроль', note: 'сайты для взрослых' },
  { k: 'safesearch', name: 'Безопасный поиск', note: 'Google, Яндекс, YouTube' },
  { k: 'dnssec', name: 'DNSSEC', note: 'проверять подписи ответов' },
  { k: 'noAaaa', name: 'Не отвечать на AAAA', note: 'отключить IPv6-адреса' },
]
const RESET = {
  cache: { title: 'Очистить кэш DNS?', text: 'Сохранённые ответы удалятся, первые запросы после этого пойдут на серверы выше и будут чуть медленнее.', ok: 'Очистить', done: 'Кэш DNS очищен' },
  log: { title: 'Сбросить журнал?', text: 'Все записи журнала запросов удалятся. Восстановить их будет нельзя.', ok: 'Сбросить', done: 'Журнал очищен' },
  stats: { title: 'Сбросить статистику?', text: 'Все записи статистики удалятся. Восстановить их будет нельзя.', ok: 'Сбросить', done: 'Статистика сброшена' },
}

const { data, load, toast, busy, act, change } = useDns<View>('/api/dns/settings')
const ask = useAsk()
// Пока серверы сохраняются, поле только для чтения: ответ иначе затёр бы допечатанное.
const ups = ref('')
watch(() => data.value?.upstreams, v => v !== undefined && (ups.value = v), { immediate: true })
const patch = (body: Partial<Record<keyof View, unknown>>, done?: string) => change(Object.keys(body)[0]!, 'PATCH', body, done)

function tog(k: Tog) {
  data.value![k] = !data.value![k]
  void patch({ [k]: data.value![k] })
}
function saveUps() {
  const norm = ups.value.split('\n').map(x => x.trim()).filter(Boolean).join('\n')
  if (data.value && norm !== data.value.upstreams) void patch({ upstreams: ups.value }, 'Серверы выше: сохранено')
}
const check = () => act('check', async () => {
  const r = await $fetch<{ total: number, failed: string[] }>('/api/dns/settings/upstreams-check', { method: 'POST', body: { upstreams: ups.value, bootstrap: data.value?.bootstrap ?? '' } })
  if (r.failed.length) toast.add({ title: `Не отвечают: ${r.failed.join(', ')}`, color: 'error' })
  else toast.add({ title: r.total === 1 ? 'Сервер отвечает' : `Все ${r.total} ${plural(r.total, 'server')} отвечают` })
})

const edit = reactive({ open: false, k: 'bootstrap' as Edit, name: '', mono: false, val: '' })
const openEdit = (k: Edit, name: string, mono = false) => Object.assign(edit, { open: true, k, name, mono, val: data.value?.[k] ?? '' })
async function saveEdit() {
  if (!busy.value.has(edit.k) && await patch({ [edit.k]: edit.val }, `${edit.name}: сохранено`)) edit.open = false
}
function reset(what: keyof typeof RESET) {
  const a = RESET[what]
  const req = what === 'cache' ? () => $fetch('/api/dns/settings/cache-clear', { method: 'POST' }) : () => $fetch('/api/dns/settings/reset', { method: 'POST', body: { what } })
  async function run() {
    await req()
    toast.add({ title: a.done })
  }
  ask.value = { title: a.title, text: a.text, ok: a.ok, danger: what !== 'cache', run }
}
</script>

<template>
  <div>
    <DnsLoadError :of="load" />
    <div v-if="data" class="sgrid">
      <div class="card sc">
        <div class="sh">
          <h3>Серверы выше</h3>
          <button type="button" class="pill" :disabled="busy.has('check')" @click="check">
            Проверить
          </button>
        </div>
        <textarea v-model="ups" class="area mono" spellcheck="false" aria-label="Серверы выше" :readonly="busy.has('upstreams')" @blur="saveUps" />
        <Seg :model-value="data.mode" small :options="MODES" class="mt-3" @update:model-value="v => patch({ mode: v })" />
        <div class="fld">
          <span>Bootstrap</span><code class="mono break-all">{{ data.bootstrap }}</code>
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
          <button type="button" role="switch" class="tog" :class="{ on: data[p.k] }" :aria-checked="data[p.k]" :aria-label="p.name" :disabled="busy.has(p.k)" @click="tog(p.k)">
            <i />
          </button>
        </div>
      </div>
      <div class="card sc">
        <div class="sh">
          <h3>Кэш и ограничения</h3>
        </div>
        <div class="fld">
          <span>Размер кэша</span><code>{{ data.cache }}</code>
          <button type="button" class="pill" @click="reset('cache')">
            Очистить
          </button>
        </div>
        <div class="fld">
          <span>Оптимистичный кэш</span><code>отвечать устаревшим, пока обновляется</code>
          <button type="button" role="switch" class="tog" :class="{ on: data.optimistic }" :aria-checked="data.optimistic" aria-label="Оптимистичный кэш" :disabled="busy.has('optimistic')" @click="tog('optimistic')">
            <i />
          </button>
        </div>
        <div v-for="[k, name] in ([['ratelimit', 'Лимит запросов'], ['block', 'Ответ на блокировку']] as const)" :key="k" class="fld">
          <span>{{ name }}</span><code class="break-all">{{ data[k] }}</code>
          <button type="button" class="pill" @click="openEdit(k, name)">
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
          <Seg :model-value="String(data.logHours)" small :options="LOG" @update:model-value="v => patch({ logHours: Number(v) })" />
        </div>
        <div class="fld">
          <span>Хранить статистику</span>
          <Seg :model-value="String(data.statsHours)" small :options="STATS" @update:model-value="v => patch({ statsHours: Number(v) })" />
        </div>
        <div class="fld">
          <span>Скрывать адреса клиентов</span><code>в журнале последний байт обнуляется</code>
          <button type="button" role="switch" class="tog" :class="{ on: data.anonymize }" :aria-checked="data.anonymize" aria-label="Скрывать адреса клиентов" :disabled="busy.has('anonymize')" @click="tog('anonymize')">
            <i />
          </button>
        </div>
        <div class="fld">
          <span>Сбросить</span><code />
          <button type="button" class="pill" @click="reset('log')">
            Журнал
          </button>
          <button type="button" class="pill" @click="reset('stats')">
            Статистику
          </button>
        </div>
      </div>
    </div>

    <Dlg v-model:open="edit.open" :title="edit.name" ok="Сохранить" :busy="busy.has(edit.k)" @ok="saveEdit">
      <label class="fl2">
        <span>{{ edit.name }}</span>
        <input v-model="edit.val" class="in" :class="{ mono: edit.mono }" spellcheck="false" @keydown.enter="saveEdit">
      </label>
    </Dlg>
  </div>
</template>
