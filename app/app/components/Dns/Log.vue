<script setup lang="ts">
// Журнал запросов: фильтр, поиск по домену и клиенту, блокировка и
// разблокировка из строки своим правилом AdGuard.
type Kind = 'ok' | 'blk' | 'rw' | 'safe'
type Filter = 'all' | 'blk' | 'ok' | 'rw'
interface Row { time: string, domain: string, type: string, kind: Kind, detail: string, client: string, ip: string }

const FILTERS = [
  { value: 'all', label: 'Все' },
  { value: 'blk', label: 'Заблокированные' },
  { value: 'ok', label: 'Разрешённые' },
  { value: 'rw', label: 'Перезаписанные' },
] as const
const TAGS: Record<Kind, { cls: string, text: string }> = {
  ok: { cls: 'tag ok', text: 'разрешён' },
  blk: { cls: 'tag badt', text: 'заблокирован' },
  rw: { cls: 'tag', text: 'перезаписан' },
  safe: { cls: 'tag badt', text: 'угроза' },
}

const toast = useToast()
const f = ref<Filter>('all')
const q = ref('')
const rows = ref<Row[]>([])
const older = ref<string | null>(null)
const loading = ref(false)
const error = ref('')
const busy = ref<string | null>(null)
const list = ref<HTMLElement>()
const pill = ref<HTMLElement>()
let seq = 0

const errText = (e: unknown) => {
  const x = e as { data?: { message?: string }, statusMessage?: string, message?: string }
  return x.data?.message ?? x.statusMessage ?? x.message ?? 'Ошибка'
}

async function load(more = false) {
  const my = ++seq
  loading.value = true
  try {
    const r = await $fetch<{ rows: Row[], older: string | null }>('/api/dns/log', {
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

function wave() {
  requestAnimationFrame(() => [...(list.value?.children ?? [])].slice(0, 12).forEach((r, i) =>
    anim(r, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], { duration: 300, delay: i * 28 })))
}

async function reload() {
  await load()
  await nextTick()
  wave()
}

let typing: ReturnType<typeof setTimeout> | undefined
watch(q, () => {
  clearTimeout(typing)
  typing = setTimeout(() => void load(), 300)
})
watch(f, () => void reload())

async function refreshLog() {
  const b = pill.value
  if (b) {
    b.classList.remove('spin')
    void b.offsetWidth
    b.classList.add('spin')
  }
  await reload()
  if (!error.value) toast.add({ title: 'Журнал обновлён' })
}

function onScroll() {
  const el = list.value
  if (!el || loading.value || !older.value) return
  if (el.scrollTop + el.clientHeight > el.scrollHeight - 200) void load(true)
}

async function act(r: Row) {
  const block = r.kind === 'ok'
  busy.value = r.domain
  try {
    await $fetch('/api/dns/log-rule', { method: 'POST', body: { domain: r.domain, block, client: r.ip } })
    for (const x of rows.value) {
      if (x.domain === r.domain && x.kind !== 'rw') {
        x.kind = block ? 'blk' : 'ok'
        x.detail = 'Свои правила'
      }
    }
    toast.add({ title: `${r.domain}: ${block ? 'заблокирован' : 'разблокирован'} своим правилом` })
  }
  catch (e) {
    toast.add({ title: errText(e), color: 'error' })
  }
  finally {
    busy.value = null
  }
}

const today = new Date().toDateString()
const two = (x: number) => String(x).padStart(2, '0')
function when(t: string) {
  const d = new Date(t)
  const hms = `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`
  return d.toDateString() === today ? hms : `${two(d.getDate())}.${two(d.getMonth() + 1)} ${hms.slice(0, 5)}`
}

onMounted(() => void load())
</script>

<template>
  <div>
    <div class="tbar">
      <label class="inp">
        <svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" /></svg>
        <input v-model="q" placeholder="Домен или клиент" aria-label="Домен или клиент">
      </label>
      <DnsSeg v-model="f" :options="FILTERS" small @pick="v => f = v" />
      <button ref="pill" type="button" class="pill" style="margin-left:auto" @click="refreshLog">
        <svg class="i" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5" /></svg>Обновить
      </button>
    </div>
    <div class="card logc">
      <div class="lt">
        <div class="th">
          <span>Время</span><span>Запрос</span><span>Ответ</span><span>Клиент</span><span />
        </div>
      </div>
      <div ref="list" class="lt scroll" @scroll.passive="onScroll">
        <div v-for="(r, i) in rows" :key="`${r.time}|${r.domain}|${r.ip}|${i}`" :class="{ blk: r.kind === 'blk' || r.kind === 'safe' }">
          <time :datetime="r.time">{{ when(r.time) }}</time>
          <div style="min-width:0">
            <b>{{ r.domain }}</b><small>{{ r.type }}</small>
          </div>
          <div class="rs">
            <span :class="TAGS[r.kind].cls">{{ TAGS[r.kind].text }}</span>
            <div><small style="margin:0">{{ r.detail }}</small></div>
          </div>
          <div style="min-width:0">
            <b style="font-weight:400">{{ r.client }}</b><small class="mono" style="font-size:11px!important">{{ r.ip }}</small>
          </div>
          <span v-if="r.kind === 'rw'" />
          <button v-else type="button" class="pill ac" :disabled="busy === r.domain" @click="act(r)">
            {{ r.kind === 'ok' ? 'Заблокировать' : 'Разблокировать' }}
          </button>
        </div>
        <div v-if="!rows.length && !loading">
          <span /><span style="color:var(--t3)">{{ error || 'ничего не нашлось' }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
