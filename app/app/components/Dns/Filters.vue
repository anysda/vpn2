<script setup lang="ts">
// Чёрные и белые списки AdGuard: включить, добавить, удалить, проверить
// обновления. Каждое действие возвращает списки из /control/filtering/status.
import { pluralRu } from '#shared/plural'

interface List { name: string, url: string, enabled: boolean, rules: number, updated: string | null }
interface View { interval: number, black: List[], white: List[] }

const toast = useToast()
const { data, error } = useFetch<View>('/api/dns/filters', { server: false, lazy: true })
const busy = ref(new Set<string>())
const updating = ref(false)

const errText = (e: unknown) => {
  const x = e as { data?: { message?: string }, statusMessage?: string, message?: string }
  return x.data?.message ?? x.statusMessage ?? x.message ?? 'Ошибка'
}

const key = (l: List, white: boolean) => `${white ? 'w' : 'b'}|${l.url}`

function every(h: number) {
  if (!h) return 'не обновляются'
  if (h < 24) return h === 1 ? 'обновляются раз в час' : `обновляются раз в ${h} ${pluralRu(h, ['час', 'часа', 'часов'])}`
  const d = Math.round(h / 24)
  if (d === 1) return 'обновляются раз в сутки'
  if (d === 7) return 'обновляются раз в неделю'
  return `обновляются раз в ${d} ${pluralRu(d, ['день', 'дня', 'дней'])}`
}

const sub = computed(() => {
  const v = data.value
  if (!v) return ''
  const on = v.black.filter(l => l.enabled)
  const rules = on.reduce((a, l) => a + l.rules, 0)
  return `Включено ${on.length} из ${v.black.length} · ${grp(rules)} ${pluralRu(rules, ['правило', 'правила', 'правил'])} · ${every(v.interval)}`
})

const two = (x: number) => String(x).padStart(2, '0')
function updated(l: List) {
  if (!l.enabled) return 'выключен'
  if (!l.updated) return '—'
  const d = new Date(l.updated)
  const hm = `${two(d.getHours())}:${two(d.getMinutes())}`
  const day = new Date()
  if (d.toDateString() === day.toDateString()) return `сегодня ${hm}`
  day.setDate(day.getDate() - 1)
  if (d.toDateString() === day.toDateString()) return `вчера ${hm}`
  return `${two(d.getDate())}.${two(d.getMonth() + 1)} ${hm}`
}

async function toggle(l: List, white: boolean) {
  const k = key(l, white)
  busy.value.add(k)
  const want = !l.enabled
  l.enabled = want
  try {
    data.value = await $fetch<View>('/api/dns/filters', { method: 'PATCH', body: { url: l.url, white, enabled: want } })
  }
  catch (e) {
    l.enabled = !want
    toast.add({ title: errText(e), color: 'error' })
  }
  finally {
    busy.value.delete(k)
  }
}

async function check() {
  updating.value = true
  try {
    const r = await $fetch<View & { updated: number }>('/api/dns/filters/refresh', { method: 'POST' })
    data.value = { interval: r.interval, black: r.black, white: r.white }
    toast.add({ title: r.updated
      ? `Списки обновлены: ${r.updated} ${pluralRu(r.updated, ['список', 'списка', 'списков'])}`
      : 'Списки обновлены, новых правил нет' })
  }
  catch (e) {
    toast.add({ title: errText(e), color: 'error' })
  }
  finally {
    updating.value = false
  }
}

// Добавление: одно окно на оба раздела, раздел запоминается при открытии.
const addOpen = ref(false)
const addWhite = ref(false)
const addName = ref('')
const addUrl = ref('')
const adding = ref(false)
const addReady = computed(() => !!addName.value.trim() && !!addUrl.value.trim() && !adding.value)

function openAdd(white: boolean) {
  addWhite.value = white
  addName.value = ''
  addUrl.value = ''
  addOpen.value = true
}

async function add() {
  if (!addReady.value) return
  adding.value = true
  const name = addName.value.trim()
  try {
    data.value = await $fetch<View>('/api/dns/filters', { method: 'POST', body: { name, url: addUrl.value.trim(), white: addWhite.value } })
    addOpen.value = false
    toast.add({ title: `Список «${name}» добавлен` })
  }
  catch (e) {
    toast.add({ title: errText(e), color: 'error' })
  }
  finally {
    adding.value = false
  }
}

// Удаление: подтверждение, строка гаснет, потом уходит из списка.
const delOpen = ref(false)
const delList = ref<{ l: List, white: boolean, row: HTMLElement | null } | null>(null)

function askDel(l: List, white: boolean, e: MouseEvent) {
  delList.value = { l, white, row: (e.currentTarget as HTMLElement).closest<HTMLElement>('.ftab > div') }
  delOpen.value = true
}

async function del() {
  const d = delList.value
  delOpen.value = false
  if (!d) return
  try {
    const v = await $fetch<View>('/api/dns/filters', { method: 'DELETE', body: { url: d.l.url, white: d.white } })
    await anim(d.row, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.94)' }], { duration: 160, easing: EO, fill: 'forwards' })?.finished
    data.value = v
    toast.add({ title: `Список «${d.l.name}» удалён` })
  }
  catch (e) {
    toast.add({ title: errText(e), color: 'error' })
  }
}

const sections = computed(() => data.value
  ? [
      { white: false, title: 'Чёрные списки', sub: sub.value, rows: data.value.black },
      { white: true, title: 'Белые списки', sub: 'Адреса отсюда не блокируются никогда', rows: data.value.white },
    ]
  : [])
</script>

<template>
  <div>
    <div v-if="error && !data" class="card">
      <h3>{{ errText(error) }}</h3>
    </div>
    <div v-for="s in sections" :key="s.title" class="card fl">
      <div class="fhead">
        <div>
          <h3>{{ s.title }}</h3>
          <p class="sub2">
            {{ s.sub }}
          </p>
        </div>
        <div v-if="!s.white" class="ha">
          <button type="button" class="pill" :disabled="updating" @click="check">
            <svg class="i" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5" /></svg>Проверить обновления
          </button>
          <button type="button" class="pill w" @click="openAdd(false)">
            <svg class="i" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>Добавить список
          </button>
        </div>
        <button v-else type="button" class="pill" @click="openAdd(true)">
          <svg class="i" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>Добавить список
        </button>
      </div>
      <div v-if="!s.white" class="ftab">
        <div class="th">
          <span /><span>Список</span><span>Правил</span><span>Обновлён</span><span />
        </div>
      </div>
      <div class="ftab">
        <div v-for="l in s.rows" :key="l.url" :class="{ off: !l.enabled }">
          <button
            type="button"
            class="tog"
            :class="{ on: l.enabled }"
            role="switch"
            :aria-checked="l.enabled"
            :aria-label="l.name"
            :disabled="busy.has(key(l, s.white))"
            @click="toggle(l, s.white)"
          >
            <i />
          </button>
          <div style="min-width:0">
            <b>{{ l.name }}</b><small class="mono" style="font-size:11px!important">{{ l.url.replace(/^https?:\/\//, '') }}</small>
          </div>
          <span>{{ l.enabled && l.rules ? grp(l.rules) : '—' }}</span>
          <span>{{ updated(l) }}</span>
          <button type="button" class="xb" aria-label="Удалить" @click="askDel(l, s.white, $event)">
            <svg class="i" viewBox="0 0 24 24"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13" /></svg>
          </button>
        </div>
      </div>
    </div>

    <DnsModal v-model:open="addOpen" title="Добавить список" close-btn>
      <label class="fl2"><span>Название</span><input v-model="addName" class="in" placeholder="напр. HaGeZi Light" @keydown.enter="add"></label>
      <label class="fl2"><span>Адрес списка</span><input v-model="addUrl" class="in mono" placeholder="https://…" @keydown.enter="add"></label>
      <template #foot="{ close }">
        <button type="button" class="pill" @click="close">
          Отмена
        </button>
        <button type="button" class="pill w" :disabled="!addReady" @click="add">
          Добавить
        </button>
      </template>
    </DnsModal>

    <DnsModal v-model:open="delOpen" title="Удалить список?">
      <p class="mt">
        Удалить список «{{ delList?.l.name }}»? Его правила перестанут применяться.
      </p>
      <template #foot="{ close }">
        <button type="button" class="pill" @click="close">
          Отмена
        </button>
        <button type="button" class="pill redw" @click="del">
          Удалить
        </button>
      </template>
    </DnsModal>
  </div>
</template>
