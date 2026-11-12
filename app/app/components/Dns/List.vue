<script setup lang="ts" generic="R extends { enabled: boolean }">
// Список AdGuard с тумблером в строке, удалением через вопрос и окном добавления.
const props = defineProps<{
  title: string
  sub: string
  rows: R[]
  cols: string[]
  grid: string
  addLabel: string
  fields: { k: string, label: string, placeholder: string, mono?: boolean }[]
  rowKey: (r: R) => string
  ask: (r: R) => { title: string, text: string }
  toggle: (r: R) => Promise<boolean>
  remove: (r: R) => Promise<boolean>
  add: (v: Record<string, string>) => Promise<boolean>
}>()

const question = useAsk()
const busy = ref(new Set<string>())
const form = reactive<{ open: boolean, v: Record<string, string> }>({ open: false, v: {} })
const ready = computed(() => props.fields.every(f => form.v[f.k]?.trim()) && !busy.value.has('add'))

async function flip(r: R) {
  const k = props.rowKey(r)
  busy.value.add(k)
  r.enabled = !r.enabled
  if (!await props.toggle(r)) r.enabled = !r.enabled
  busy.value.delete(k)
}
function askRemove(r: R) {
  const a = props.ask(r)
  question.value = { title: a.title, text: a.text, ok: 'Удалить', danger: true, run: () => props.remove(r) }
}
async function save() {
  if (!ready.value) return
  busy.value.add('add')
  if (await props.add(form.v)) form.open = false
  busy.value.delete('add')
}
</script>

<template>
  <div class="card mb-3">
    <DnsHead :title="title" :sub="sub">
      <div class="ha">
        <slot name="actions" />
        <button type="button" class="pill w" @click="Object.assign(form, { open: true, v: {} })">
          <svg class="i" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>{{ addLabel }}
        </button>
      </div>
    </DnsHead>
    <div v-if="cols.length" class="grid items-center gap-3 px-1 py-2 text-xs text-(--t3) max-md:hidden" :class="grid">
      <span v-for="(c, i) in cols" :key="i">{{ c }}</span>
    </div>
    <div v-for="r in rows" :key="rowKey(r)" class="grid items-center gap-3 border-t border-(--line) px-1 py-2.5 text-sm" :class="[grid, { 'opacity-50': !r.enabled }]">
      <button type="button" class="tog" :class="{ on: r.enabled }" role="switch" :aria-checked="r.enabled" :aria-label="rowKey(r)" :disabled="busy.has(rowKey(r))" @click="flip(r)">
        <i />
      </button>
      <slot :row="r" />
      <button type="button" class="xb" aria-label="Удалить" @click="askRemove(r)">
        <svg class="i" viewBox="0 0 24 24"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13" /></svg>
      </button>
    </div>
    <PanelDialog v-model:open="form.open" :title="addLabel" ok="Добавить" :busy="!ready" @ok="save">
      <label v-for="f in fields" :key="f.k" class="fl2">
        <span>{{ f.label }}</span>
        <input v-model="form.v[f.k]" class="in" :class="{ mono: f.mono }" :placeholder="f.placeholder" spellcheck="false" @keydown.enter="save">
      </label>
    </PanelDialog>
  </div>
</template>
