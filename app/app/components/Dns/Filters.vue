<script setup lang="ts">
// Чёрные и белые списки AdGuard: включить, добавить, удалить, проверить
// обновления. Каждое действие возвращает списки из /control/filtering/status.
import { plural } from '#shared/plural'

interface List { name: string, url: string, enabled: boolean, rules: number, updated: string | null }
interface View { interval: number, black: List[], white: List[] }

const { data, load, busy, api, change, toggleRow, removeRow } = useDnsSection<View>('/api/dns/filters')

function every(h: number) {
  if (!h) return 'не обновляются'
  if (h < 24) return h === 1 ? 'обновляются раз в час' : `обновляются раз в ${h} ${plural(h, 'hour')}`
  const d = Math.round(h / 24)
  if (d === 1) return 'обновляются раз в сутки'
  if (d === 7) return 'обновляются раз в неделю'
  return `обновляются раз в ${d} ${plural(d, 'day')}`
}

const sections = computed(() => {
  const v = data.value
  if (!v) return []
  const on = v.black.filter(l => l.enabled)
  const rules = on.reduce((a, l) => a + l.rules, 0)
  const sub = `Включено ${on.length} из ${v.black.length} · ${ru(rules)} ${plural(rules, 'rule')} · ${every(v.interval)}`
  return [
    { white: false, title: 'Чёрные списки', sub, rows: v.black },
    { white: true, title: 'Белые списки', sub: 'Адреса отсюда не блокируются никогда', rows: v.white },
  ]
})

function updated(l: List) {
  if (!l.enabled) return 'выключен'
  return l.updated ? todayYesterdayHmLocal(l.updated) : '—'
}

// Строка телефона: правил и обновление — одной строкой под адресом.
const meta = (l: List) => l.enabled && l.rules ? `${ru(l.rules)} ${plural(l.rules, 'rule')} · ${updated(l)}` : updated(l)

const check = () => change('check', api<View & { updated: number }>('POST', undefined, '/refresh'), r => r.updated
  ? `Списки обновлены: ${r.updated} ${plural(r.updated, 'list')}`
  : 'Списки обновлены, новых правил нет')

// Добавление: одно окно на оба раздела, раздел запоминается при открытии.
const add = reactive({ open: false, white: false, name: '', url: '' })
const addReady = computed(() => !!add.name.trim() && !!add.url.trim() && !busy.value.has('add'))

function openAdd(white: boolean) {
  Object.assign(add, { open: true, white, name: '', url: '' })
}

async function save() {
  if (!addReady.value) return
  const name = add.name.trim()
  const sent = await change('add', api('POST', { name, url: add.url.trim(), white: add.white }), `Список «${name}» добавлен`)
  if (sent) add.open = false
}

// Удаление: подтверждение, строка гаснет, потом уходит из списка.
const del = reactive({ open: false, white: false, l: null as List | null, row: null as HTMLElement | null })

function askDel(l: List, white: boolean, e: MouseEvent) {
  Object.assign(del, { open: true, white, l, row: (e.currentTarget as HTMLElement).closest('.ftab > div') })
}

function confirmDel() {
  del.open = false
  if (del.l) void removeRow(del.row, { url: del.l.url, white: del.white }, `Список «${del.l.name}» удалён`)
}
</script>

<template>
  <div>
    <DnsLoadError :of="load" />
    <div v-for="s in sections" :key="s.title" class="card fl">
      <DnsHead :title="s.title" :sub="s.sub">
        <div v-if="!s.white" class="ha">
          <button type="button" class="pill" :disabled="busy.has('check')" @click="check">
            <svg class="i" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5" /></svg>Проверить обновления
          </button>
          <button type="button" class="pill w" @click="openAdd(false)">
            <svg class="i" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>Добавить список
          </button>
        </div>
        <button v-else type="button" class="pill" @click="openAdd(true)">
          <svg class="i" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>Добавить список
        </button>
      </DnsHead>
      <div v-if="!s.white" class="ftab">
        <div class="th">
          <span /><span>Список</span><span>Правил</span><span>Обновлён</span><span />
        </div>
      </div>
      <div class="ftab">
        <div v-for="l in s.rows" :key="l.url" :class="{ off: !l.enabled }">
          <button
            type="button" class="tog" :class="{ on: l.enabled }" role="switch" :aria-checked="l.enabled" :aria-label="l.name"
            :disabled="busy.has(`${s.white}|${l.url}`)" @click="toggleRow(l, `${s.white}|${l.url}`, { url: l.url, white: s.white })">
            <i />
          </button>
          <div style="min-width:0">
            <b>{{ l.name }}</b><small class="mono" style="font-size:11px!important">{{ l.url.replace(/^https?:\/\//, '') }}</small>
            <small class="fm">{{ meta(l) }}</small>
          </div>
          <span>{{ l.enabled && l.rules ? ru(l.rules) : '—' }}</span>
          <span>{{ updated(l) }}</span>
          <button type="button" class="xb" aria-label="Удалить" @click="askDel(l, s.white, $event)">
            <svg class="i" viewBox="0 0 24 24"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13" /></svg>
          </button>
        </div>
      </div>
    </div>

    <PanelModal v-model:open="add.open" class="dns" title="Добавить список" closable ok="Добавить" :ok-disabled="!addReady" @ok="save">
      <label class="fl2"><span>Название</span><input v-model="add.name" class="in" placeholder="напр. HaGeZi Light" @keydown.enter="save"></label>
      <label class="fl2"><span>Адрес списка</span><input v-model="add.url" class="in mono" placeholder="https://…" @keydown.enter="save"></label>
    </PanelModal>

    <PanelModal v-model:open="del.open" class="dns" title="Удалить список?" ok="Удалить" ok-class="redw" @ok="confirmDel">
      <p class="mt">
        Удалить список «{{ del.l?.name }}»? Его правила перестанут применяться.
      </p>
    </PanelModal>
  </div>
</template>
