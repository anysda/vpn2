<script setup lang="ts">
// Перезаписи DNS AdGuard: свой ответ на запрос. Каждое действие возвращает
// список из /control/rewrite/list — показываем то, что AdGuard применил.
interface Rewrite { domain: string, answer: string, enabled: boolean }

const { data, load, busy, api, change, toggleRow, removeRow } = useDnsSection<Rewrite[]>('/api/dns/rewrites')
const key = (r: Rewrite) => `${r.domain}|${r.answer}`

const add = reactive({ open: false, domain: '', answer: '' })
const addReady = computed(() => !!add.domain.trim() && !!add.answer.trim() && !busy.value.has('add'))

function openAdd() {
  Object.assign(add, { open: true, domain: '', answer: '' })
}

async function save() {
  if (!addReady.value) return
  const done = `Перезапись «${add.domain.trim().toLowerCase().replace(/\.$/, '')}» добавлена`
  if (await change('add', api('POST', { domain: add.domain, answer: add.answer }), done)) add.open = false
}

const del = reactive({ open: false, r: null as Rewrite | null, row: null as HTMLElement | null })

function askDel(r: Rewrite, e: MouseEvent) {
  Object.assign(del, { open: true, r, row: (e.currentTarget as HTMLElement).closest('.rwt > div') })
}

function confirmDel() {
  del.open = false
  if (del.r) void removeRow(del.row, { domain: del.r.domain, answer: del.r.answer }, `Перезапись «${del.r.domain}» удалена`)
}
</script>

<template>
  <div>
    <DnsLoadError :of="load" />
    <div class="card fl">
      <DnsHead title="Перезаписи DNS" sub="Свой ответ на запрос: внутренние адреса, заглушки">
        <button type="button" class="pill w" @click="openAdd">
          <svg class="i" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>Добавить перезапись
        </button>
      </DnsHead>
      <div class="rwt">
        <div class="th">
          <span /><span>Домен</span><span /><span>Ответ</span><span>Зачем</span><span />
        </div>
      </div>
      <div class="rwt">
        <div v-for="r in data ?? []" :key="key(r)" :class="{ off: !r.enabled }">
          <button
            type="button" class="tog" :class="{ on: r.enabled }" role="switch" :aria-checked="r.enabled" :aria-label="r.domain"
            :disabled="busy.has(key(r))" @click="toggleRow(r, key(r), { domain: r.domain, answer: r.answer })">
            <i />
          </button>
          <span class="mono">{{ r.domain }}</span>
          <span class="arr">→</span>
          <span class="mono">{{ r.answer }}</span>
          <span>—</span>
          <button type="button" class="xb" aria-label="Удалить" @click="askDel(r, $event)">
            <svg class="i" viewBox="0 0 24 24"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13" /></svg>
          </button>
        </div>
      </div>
    </div>

    <PanelModal v-model:open="add.open" class="dns" title="Добавить перезапись" closable ok="Добавить" :ok-disabled="!addReady" @ok="save">
      <label class="fl2"><span>Домен</span><input v-model="add.domain" class="in mono" placeholder="напр. nas.home" spellcheck="false" @keydown.enter="save"></label>
      <label class="fl2"><span>Ответ</span><input v-model="add.answer" class="in mono" placeholder="IP или домен" spellcheck="false" @keydown.enter="save"></label>
    </PanelModal>

    <PanelModal v-model:open="del.open" class="dns" title="Удалить перезапись?" ok="Удалить" ok-class="redw" @ok="confirmDel">
      <p class="mt">
        Удалить перезапись «{{ del.r?.domain }}»? Запрос снова пойдёт на серверы выше.
      </p>
    </PanelModal>
  </div>
</template>
