<script setup lang="ts">
// Экран 1: ключ оркестратора и новый экзит - общий экран панели (ui/ExitAdd.vue), экзиты черновика
// рядами с итогом проверки, ключ ssh человека. Сервер проверяет строку черновика: проверка из формы
// сперва кладёт в черновик её строку. Не годен - проверить снова, заменить адрес или без него.
import { computed, ref } from 'vue'
import ExitAdd from '../../ui/ExitAdd.vue'
import { exitGood, exitsReady, putDraft, refusal, transport, type DraftExit, type State } from '../api'

const props = defineProps<{ state: State }>()

const exits = ref<DraftExit[]>(props.state.draft.exits)
const adminKey = ref(props.state.draft.admin_ssh_key)
const busy = ref('')
const error = ref('')
const editing = ref('')
const newHost = ref('')
// Строка, которую положила в черновик проверка из формы: повторная проверка формы заменяет её.
let pending: { tag: string, host: string } | null = null

async function put(next: DraftExit[]) {
  exits.value = (await putDraft({ exits: next })).exits
}
const row = (tag: string, host: string) => ({ tag, host, skipped: false, problems: null })
const isPending = (e: DraftExit) => !!pending && e.tag === pending.tag && e.host === pending.host

async function checked(tag: string) {
  const problems = await transport.check(tag, '')
  exits.value = exits.value.map(e => e.tag === tag ? { ...e, problems } : e)
  return problems
}

const formTransport = {
  ...transport,
  // Отказ сервера (формат, тег уже есть) форма показывает текстом, как и сбой проверки.
  check: async (tag: string, host: string) => {
    await put([...exits.value.filter(e => !isPending(e)), row(tag, host)])
    pending = { tag, host }
    return checked(tag)
  },
}

async function act(run: () => Promise<unknown>, tag = '') {
  busy.value = tag || '*'
  error.value = ''
  try {
    await run()
    return true
  }
  catch (e) {
    error.value = refusal(e)
    return false
  }
  finally {
    busy.value = ''
  }
}

async function finish(skip: boolean, reset: () => void) {
  const done = await act(() => skip ? put(exits.value.map(e => isPending(e) ? { ...e, skipped: true } : e)) : Promise.resolve())
  if (done) {
    pending = null
    reset()
  }
}

const setSkipped = (exit: DraftExit, skipped: boolean) => put(exits.value.map(e => e === exit ? { ...e, skipped } : e))

const recheck = (exit: DraftExit) => act(async () => {
  const problems = await checked(exit.tag)
  if (!problems.length && exit.skipped) await setSkipped(exits.value.find(e => e.tag === exit.tag)!, false)
}, exit.tag)

const replace = (exit: DraftExit) => act(async () => {
  await put(exits.value.map(e => e === exit ? row(exit.tag, newHost.value.trim()) : e))
  editing.value = ''
  await checked(exit.tag)
}, exit.tag)

function edit(exit: DraftExit) {
  editing.value = exit.tag
  newHost.value = exit.host
}

const label = (e: DraftExit) => exitGood(e) ? 'годен' : e.skipped ? 'без него' : e.problems ? 'не годен' : 'не проверен'
const ready = computed(() => !!adminKey.value.trim() && exitsReady(exits.value))
defineExpose({ ready, collect: () => ({ patch: { admin_ssh_key: adminKey.value.trim() } }) })
</script>

<template>
  <section>
    <ExitAdd :transport="formTransport">
      <template #submit="{ ready: good, problems, reset }">
        <p v-if="problems?.length" class="mt">
          Исправьте на сервере и проверьте снова, замените адрес в поле выше или продолжите без этого экзита: его можно добавить позже в настройках панели.
        </p>
        <div class="fld">
          <span /><code />
          <button v-if="problems?.length" type="button" class="pill" :disabled="!!busy" @click="finish(true, reset)">Продолжить без него</button>
          <button type="button" class="pill w" :disabled="!good || !!busy" @click="finish(false, reset)">Добавить</button>
        </div>
      </template>
    </ExitAdd>

    <div class="wzsec">
      <h3>Экзиты установки</h3>
      <p v-if="!exits.length" class="sub2">Пока ни одного: добавьте экзит выше.</p>
      <div v-for="exit in exits" :key="`${exit.tag}-${exit.host}`" class="wzrow" data-testid="exit-row">
        <div class="fld">
          <span class="mono">{{ exit.tag.toUpperCase() }}</span>
          <input v-if="editing === exit.tag" v-model="newHost" class="in mono" aria-label="Новый адрес" spellcheck="false">
          <code v-else class="mono">{{ exit.host }}</code>
          <span v-if="busy === exit.tag" class="tag">проверяю…</span>
          <span v-else class="tag" :class="{ ok: exitGood(exit), badt: !exitGood(exit) }">{{ label(exit) }}</span>
          <template v-if="editing === exit.tag">
            <button type="button" class="pill" :disabled="!!busy" @click="editing = ''">Отмена</button>
            <button type="button" class="pill w" :disabled="!!busy || !newHost.trim()" @click="replace(exit)">Проверить</button>
          </template>
          <template v-else>
            <template v-if="!exitGood(exit)">
              <button type="button" class="pill" :disabled="!!busy" @click="recheck(exit)">Проверить снова</button>
              <button type="button" class="pill" :disabled="!!busy" @click="edit(exit)">Заменить адрес</button>
              <button v-if="!exit.skipped" type="button" class="pill" :disabled="!!busy" @click="act(() => setSkipped(exit, true))">Без него</button>
            </template>
            <button type="button" class="pill red" :disabled="!!busy" @click="act(() => put(exits.filter(e => e !== exit)))">Убрать</button>
          </template>
        </div>
        <div v-if="exit.problems?.length" class="al red">
          <b>{{ exit.skipped ? 'Установка пойдёт без этого экзита' : 'Сервер пока не годится' }}</b>
          <span v-for="p in exit.problems" :key="p">{{ p }}</span>
        </div>
      </div>
      <p v-if="error" role="alert" class="wzbad">{{ error }}</p>
      <p v-if="exits.length && !exitsReady(exits)" class="wzbad">
        Нужен хотя бы один годный экзит, а каждый не годный - проверить снова, заменить адрес или оставить без него.
      </p>
    </div>

    <label class="fl2 wzsec">
      <span>Ваш открытый ключ ssh - обязателен. Деплой кладёт его в /root/.ssh/authorized_keys на всех серверах, вход по паролю закрыт везде</span>
      <textarea v-model="adminKey" class="area mono wzkey" placeholder="ssh-ed25519 AAAA… you@laptop" spellcheck="false" autocomplete="off" />
    </label>
  </section>
</template>
