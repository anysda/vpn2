<script setup lang="ts">
// Настройки DNS AdGuard: серверы выше, защита, кэш, журнал. Каждое изменение —
// одно поле; ответ сервера — то, что AdGuard показывает после изменения.
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
type Ask = 'cache' | 'log' | 'stats'

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
const LIMITS: { k: Edit, name: string }[] = [
  { k: 'ratelimit', name: 'Лимит запросов' },
  { k: 'block', name: 'Ответ на блокировку' },
]
// Очистка кэша и сброс журнала или статистики — через подтверждение.
const ASK: Record<Ask, { title: string, text: string, btn: string, done: string }> = {
  cache: { title: 'Очистить кэш DNS?', text: 'Сохранённые ответы удалятся, первые запросы после этого пойдут на серверы выше и будут чуть медленнее.', btn: 'Очистить', done: 'Кэш DNS очищен' },
  log: { title: 'Сбросить журнал?', text: 'Все записи журнала запросов удалятся. Восстановить их будет нельзя.', btn: 'Сбросить', done: 'Журнал очищен' },
  stats: { title: 'Сбросить статистику?', text: 'Все записи статистики удалятся. Восстановить их будет нельзя.', btn: 'Сбросить', done: 'Статистика сброшена' },
}

const { data, load, toast, busy, act, api, change } = useDnsSection<View>('/api/dns/settings')
// Пока серверы сохраняются, поле только для чтения: ответ PATCH иначе затёр
// бы допечатанное, а тост «сохранено» говорил бы не о том, что видно.
const ups = ref('')
watch(() => data.value?.upstreams, (v) => {
  if (v !== undefined) ups.value = v
}, { immediate: true })

const patch = (body: Partial<Record<keyof View, unknown>>, done?: string) =>
  change(Object.keys(body)[0]!, api('PATCH', body), done)

function tog(k: Tog) {
  if (!data.value) return
  data.value[k] = !data.value[k]
  void patch({ [k]: data.value[k] })
}

function saveUps() {
  const norm = (s: string) => s.split('\n').map(x => x.trim()).filter(Boolean).join('\n')
  if (data.value && norm(ups.value) !== data.value.upstreams) void patch({ upstreams: ups.value }, 'Серверы выше: сохранено')
}

const check = () => act('check', async () => {
  const r = await $fetch<{ total: number, failed: string[] }>('/api/dns/settings/upstreams-check', {
    method: 'POST', body: { upstreams: ups.value, bootstrap: data.value?.bootstrap ?? '' },
  })
  if (r.failed.length) toast.add({ title: `Не отвечают: ${r.failed.join(', ')}`, color: 'error' })
  else toast.add({ title: r.total === 1 ? 'Сервер отвечает' : `Все ${r.total} ${plural(r.total, 'server')} отвечают` })
})

// «Изменить»: окно с одним полем, заголовок и подпись — название строки.
const edit = reactive({ open: false, k: 'bootstrap' as Edit, name: '', mono: false, val: '' })

function openEdit(k: Edit, name: string, mono = false) {
  Object.assign(edit, { open: true, k, name, mono, val: data.value?.[k] ?? '' })
}

async function saveEdit() {
  if (busy.value.has(edit.k)) return
  if (await patch({ [edit.k]: edit.val }, `${edit.name}: сохранено`)) edit.open = false
}

const ask = reactive({ open: false, what: 'cache' as Ask })

function openAsk(what: Ask) {
  Object.assign(ask, { open: true, what })
}

function confirmAsk() {
  const a = ask.what
  ask.open = false
  void act(`ask-${a}`, () => a === 'cache'
    ? $fetch<unknown>('/api/dns/settings/cache-clear', { method: 'POST' })
    : $fetch<unknown>('/api/dns/settings/reset', { method: 'POST', body: { what: a } }), ASK[a].done)
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
        <textarea
          v-model="ups"
          class="mono"
          spellcheck="false"
          style="min-height:104px;flex:none"
          aria-label="Серверы выше"
          :readonly="busy.has('upstreams')"
          @blur="saveUps" />
        <div class="arow">
          <DnsSeg :model-value="data.mode" small :options="MODES" @pick="v => patch({ mode: v })" />
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
          <ToggleSwitch :on="data[p.k]" :label="p.name" :disabled="busy.has(p.k)" @click="tog(p.k)" />
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
          <ToggleSwitch :on="data.optimistic" label="Оптимистичный кэш" :disabled="busy.has('optimistic')" @click="tog('optimistic')" />
        </div>
        <div v-for="l in LIMITS" :key="l.k" class="fld">
          <span>{{ l.name }}</span><code><WholeWords :text="data[l.k]" /></code>
          <button type="button" class="pill" @click="openEdit(l.k, l.name)">
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
          <DnsSeg :model-value="String(data.logHours)" small :options="LOG" @pick="v => patch({ logHours: Number(v) })" />
        </div>
        <div class="fld">
          <span>Хранить статистику</span>
          <DnsSeg :model-value="String(data.statsHours)" small :options="STATS" @pick="v => patch({ statsHours: Number(v) })" />
        </div>
        <div class="fld">
          <span>Скрывать адреса клиентов</span><code>в журнале последний байт обнуляется</code>
          <ToggleSwitch :on="data.anonymize" label="Скрывать адреса клиентов" :disabled="busy.has('anonymize')" @click="tog('anonymize')" />
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

    <PanelModal v-model:open="edit.open" class="dns" :title="edit.name" closable ok="Сохранить" :ok-disabled="busy.has(edit.k)" @ok="saveEdit">
      <label class="fl2">
        <span>{{ edit.name }}</span>
        <input v-model="edit.val" class="in" :class="{ mono: edit.mono }" spellcheck="false" @keydown.enter="saveEdit">
      </label>
    </PanelModal>

    <PanelModal
v-model:open="ask.open"
      class="dns"
      :title="ASK[ask.what].title"
      :ok="ASK[ask.what].btn"
      :ok-class="ask.what === 'cache' ? 'w' : 'redw'"
      @ok="confirmAsk">
      <p class="mt">
        {{ ASK[ask.what].text }}
      </p>
    </PanelModal>
  </div>
</template>
