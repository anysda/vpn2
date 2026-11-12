<script setup lang="ts">
type Kind = 'ok' | 'blk' | 'rw' | 'safe'
type Row = Api<'/api/dns/log'>['rows'][number]

const FILTERS = [{ value: 'all', label: 'Все' }, { value: 'blk', label: 'Заблокированные' }, { value: 'ok', label: 'Разрешённые' }, { value: 'rw', label: 'Перезаписанные' }] as const
const TAGS: Record<Kind, [string, string]> = { ok: ['ok', 'разрешён'], blk: ['badt', 'заблокирован'], rw: ['', 'перезаписан'], safe: ['badt', 'угроза'] }

const { toast, busy, act } = useAct()
const f = ref<typeof FILTERS[number]['value']>('all')
const q = ref('')
const rows = ref<Row[]>([])
const older = ref<string | null>(null)
const loading = ref(false)
const error = ref('')
const today = new Date().toDateString()
let seq = 0, timer: ReturnType<typeof setTimeout> | undefined

async function load(more = false) {
  const my = ++seq
  loading.value = true
  try {
    const r = await $fetch('/api/dns/log', {
      query: { f: f.value, q: q.value.trim() || undefined, older: more ? older.value ?? undefined : undefined },
    })
    if (my !== seq) return
    rows.value = more ? [...rows.value, ...r.rows] : r.rows
    older.value = r.older
    error.value = ''
  }
  catch (e) {
    if (my === seq) error.value = errText(e)
  }
  finally {
    if (my === seq) loading.value = false
  }
}
onMounted(load)
watch(f, () => load())
watch(q, () => {
  clearTimeout(timer)
  timer = setTimeout(load, 300)
})
async function refresh() {
  await load()
  if (!error.value) toast.add({ title: 'Журнал обновлён' })
}
// Подгрузка старых записей у конца списка: прокрутка списка на ПК, страницы - на телефоне.
const list = ref<HTMLElement>()
function onScroll() {
  const el = list.value
  if (!el || loading.value || !older.value) return
  const nearOwnEnd = el.scrollTop + el.clientHeight > el.scrollHeight - 200
  const nearPageEnd = el.getBoundingClientRect().bottom < innerHeight + 200
  if (nearOwnEnd && nearPageEnd) void load(true)
}
onMounted(() => addEventListener('scroll', onScroll, { passive: true }))
onUnmounted(() => removeEventListener('scroll', onScroll))

function rule(r: Row) {
  const block = r.kind === 'ok'
  return act(r.domain, async () => {
    await $fetch<unknown>('/api/dns/log-rule', { method: 'POST', body: { domain: r.domain, block, client: r.ip } })
    for (const x of rows.value) if (x.domain === r.domain && x.kind !== 'rw') Object.assign(x, { kind: block ? 'blk' : 'ok', detail: 'Свои правила' })
  }, `${r.domain}: ${block ? 'заблокирован' : 'разблокирован'} своим правилом`)
}
</script>

<template>
  <div>
    <div class="mb-3 flex flex-wrap items-center gap-2">
      <label class="inp">
        <svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" /></svg>
        <input v-model="q" placeholder="Домен или клиент" aria-label="Домен или клиент">
      </label>
      <SegTabs v-model="f" :options="FILTERS" small />
      <button type="button" class="pill md:ml-auto" @click="refresh">
        Обновить
      </button>
    </div>
    <div ref="list" class="card scroll max-h-[70vh] max-md:max-h-none" @scroll.passive="onScroll">
      <div
        v-for="(r, i) in rows" :key="`${r.time}|${r.domain}|${r.ip}|${i}`" data-testid="dns-log-row"
        class="grid grid-cols-[80px_minmax(0,1.5fr)_minmax(0,1fr)_minmax(0,1fr)_130px] items-center gap-3 border-b border-(--line) py-2 text-sm max-md:grid-cols-[minmax(0,1fr)_auto]">
        <time class="mono text-xs text-(--t3) max-md:hidden" :datetime="r.time">{{ hmsOrDdmmHmLocal(r.time, today) }}</time>
        <div class="min-w-0">
          <b class="block truncate font-medium" :class="{ 'text-(--red-t)': r.kind === 'blk' || r.kind === 'safe' }">{{ r.domain }}</b>
          <small class="text-xs text-(--t3)">{{ r.type }}<span class="md:hidden"> · {{ hmsOrDdmmHmLocal(r.time, today) }} · {{ r.client }}</span></small>
        </div>
        <div class="min-w-0 max-md:hidden">
          <span class="tag" :class="TAGS[r.kind][0]">{{ TAGS[r.kind][1] }}</span>
          <small class="block truncate text-xs text-(--t3)">{{ r.detail }}</small>
        </div>
        <div class="min-w-0 max-md:hidden">
          <span class="block truncate">{{ r.client }}</span><small class="mono text-xs text-(--t3)">{{ r.ip }}</small>
        </div>
        <span v-if="r.kind === 'rw'" />
        <button v-else type="button" class="pill" :disabled="busy.has(r.domain)" @click="rule(r)">
          {{ r.kind === 'ok' ? 'Заблокировать' : 'Разблокировать' }}
        </button>
      </div>
      <p v-if="!rows.length && !loading" class="py-4 text-center text-sm text-(--t3)">
        {{ error || 'ничего не нашлось' }}
      </p>
    </div>
  </div>
</template>
