<script setup lang="ts">
// Перезаписи DNS AdGuard: свой ответ на запрос. Каждое действие возвращает
// список из /control/rewrite/list — показываем то, что AdGuard применил.
interface Rewrite { domain: string, answer: string, enabled: boolean }

const toast = useToast()
const { data, error, status, refresh } = useFetch<Rewrite[]>('/api/dns/rewrites', { server: false, lazy: true })
const latest = latestWins<Rewrite[]>(v => (data.value = v), () => $fetch<Rewrite[]>('/api/dns/rewrites'))
const busy = ref(new Set<string>())

const errText = (e: unknown) => {
  const x = e as { data?: { message?: string }, statusMessage?: string, message?: string }
  return x.data?.message ?? x.statusMessage ?? x.message ?? 'Ошибка'
}

const key = (r: Rewrite) => `${r.domain}|${r.answer}`

async function toggle(r: Rewrite) {
  const k = key(r)
  busy.value.add(k)
  const want = !r.enabled
  r.enabled = want
  try {
    await latest(() => $fetch<Rewrite[]>('/api/dns/rewrites', { method: 'PATCH', body: { domain: r.domain, answer: r.answer, enabled: want } }))
  }
  catch (e) {
    r.enabled = !want
    toast.add({ title: errText(e), color: 'error' })
  }
  finally {
    busy.value.delete(k)
  }
}

const addOpen = ref(false)
const addDomain = ref('')
const addAnswer = ref('')
const adding = ref(false)
const addReady = computed(() => !!addDomain.value.trim() && !!addAnswer.value.trim() && !adding.value)

function openAdd() {
  addDomain.value = ''
  addAnswer.value = ''
  addOpen.value = true
}

async function add() {
  if (!addReady.value) return
  adding.value = true
  try {
    await latest(() => $fetch<Rewrite[]>('/api/dns/rewrites', { method: 'POST', body: { domain: addDomain.value, answer: addAnswer.value } }))
    addOpen.value = false
    toast.add({ title: `Перезапись «${addDomain.value.trim().toLowerCase().replace(/\.$/, '')}» добавлена` })
  }
  catch (e) {
    toast.add({ title: errText(e), color: 'error' })
  }
  finally {
    adding.value = false
  }
}

const delOpen = ref(false)
const delRw = ref<{ r: Rewrite, row: HTMLElement | null } | null>(null)

function askDel(r: Rewrite, e: MouseEvent) {
  delRw.value = { r, row: (e.currentTarget as HTMLElement).closest<HTMLElement>('.rwt > div') }
  delOpen.value = true
}

async function del() {
  const d = delRw.value
  delOpen.value = false
  if (!d) return
  try {
    await latest(async () => {
      const v = await $fetch<Rewrite[]>('/api/dns/rewrites', { method: 'DELETE', body: { domain: d.r.domain, answer: d.r.answer } })
      await anim(d.row, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.94)' }], { duration: 160, easing: EO, fill: 'forwards' })?.finished
      return v
    })
    toast.add({ title: `Перезапись «${d.r.domain}» удалена` })
  }
  catch (e) {
    toast.add({ title: errText(e), color: 'error' })
  }
}
</script>

<template>
  <div>
    <DnsLoadError v-if="error && !data" :message="errText(error)" :pending="status === 'pending'" @retry="refresh()" />
    <div class="card fl">
      <div class="fhead">
        <div>
          <h3>Перезаписи DNS</h3>
          <p class="sub2">
            Свой ответ на запрос: внутренние адреса, заглушки
          </p>
        </div>
        <button type="button" class="pill w" @click="openAdd">
          <svg class="i" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>Добавить перезапись
        </button>
      </div>
      <div class="rwt">
        <div class="th">
          <span /><span>Домен</span><span /><span>Ответ</span><span>Зачем</span><span />
        </div>
      </div>
      <div class="rwt">
        <div v-for="r in data ?? []" :key="key(r)" :class="{ off: !r.enabled }">
          <button
            type="button"
            class="tog"
            :class="{ on: r.enabled }"
            role="switch"
            :aria-checked="r.enabled"
            :aria-label="r.domain"
            :disabled="busy.has(key(r))"
            @click="toggle(r)"
          >
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

    <DnsModal v-model:open="addOpen" title="Добавить перезапись" close-btn>
      <label class="fl2"><span>Домен</span><input v-model="addDomain" class="in mono" placeholder="напр. nas.home" spellcheck="false" @keydown.enter="add"></label>
      <label class="fl2"><span>Ответ</span><input v-model="addAnswer" class="in mono" placeholder="IP или домен" spellcheck="false" @keydown.enter="add"></label>
      <template #foot="{ close }">
        <button type="button" class="pill" @click="close">
          Отмена
        </button>
        <button type="button" class="pill w" :disabled="!addReady" @click="add">
          Добавить
        </button>
      </template>
    </DnsModal>

    <DnsModal v-model:open="delOpen" title="Удалить перезапись?">
      <p class="mt">
        Удалить перезапись «{{ delRw?.r.domain }}»? Запрос снова пойдёт на серверы выше.
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
