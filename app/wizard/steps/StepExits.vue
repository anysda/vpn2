<script setup lang="ts">
// Экран 1: ключ оркестратора и новый экзит - общий экран панели (ui/ExitAdd.vue), экзиты черновика
// рядами с итогом проверки, ключ ssh человека. Не годен - проверить снова, заменить адрес или без него.
import { computed, ref } from 'vue'
import ExitAdd from '../../ui/ExitAdd.vue'
import { call, refusal, transport, type DraftExit, type State } from '../api'

const props = defineProps<{ state: State }>()

const exits = ref<DraftExit[]>(props.state.draft.exits)
const adminKey = ref(props.state.draft.admin_ssh_key)
const busy = ref('')
const error = ref('')
const editing = ref('')
const newHost = ref('')

async function put(next: DraftExit[]) {
  error.value = ''
  try {
    await call('PUT', '/api/draft', { exits: next })
    exits.value = next
    return true
  }
  catch (e) {
    error.value = refusal(e)
    return false
  }
}

async function add(value: { tag: string, host: string }, problems: string[], reset: () => void) {
  if (await put([...exits.value, { ...value, skipped: problems.length > 0, problems }])) reset()
}

async function check(exit: DraftExit, host = exit.host) {
  busy.value = exit.tag
  error.value = ''
  try {
    const problems = await transport.check(exit.tag, host)
    if (await put(exits.value.map(e => e.tag === exit.tag ? { ...e, host, problems, skipped: problems.length > 0 } : e))) editing.value = ''
  }
  catch (e) {
    error.value = refusal(e)
  }
  finally {
    busy.value = ''
  }
}

function edit(exit: DraftExit) {
  editing.value = exit.tag
  newHost.value = exit.host
}

const ready = computed(() => !!adminKey.value.trim() && exits.value.some(e => !e.skipped))
defineExpose({ ready, collect: () => ({ patch: { admin_ssh_key: adminKey.value.trim() } }) })
</script>

<template>
  <section>
    <ExitAdd :transport="transport">
      <template #submit="{ value, ready: good, problems, reset }">
        <p v-if="problems?.length" class="mt">
          Исправьте на сервере и проверьте снова, замените адрес в поле выше или продолжите без этого экзита: его можно добавить позже в настройках панели.
        </p>
        <div class="fld">
          <span /><code />
          <button v-if="problems?.length" type="button" class="pill" @click="add(value, problems, reset)">Продолжить без него</button>
          <button type="button" class="pill w" :disabled="!good" @click="add(value, [], reset)">Добавить</button>
        </div>
      </template>
    </ExitAdd>

    <div class="wzsec">
      <h3>Экзиты установки</h3>
      <p v-if="!exits.length" class="sub2">Пока ни одного: добавьте экзит выше.</p>
      <div v-for="exit in exits" :key="exit.tag" class="wzrow" data-testid="exit-row">
        <div class="fld">
          <span class="mono">{{ exit.tag.toUpperCase() }}</span>
          <input v-if="editing === exit.tag" v-model="newHost" class="in mono" aria-label="Новый адрес" spellcheck="false">
          <code v-else class="mono">{{ exit.host }}</code>
          <span v-if="busy === exit.tag" class="tag">проверяю…</span>
          <span v-else-if="exit.skipped" class="tag badt">без него</span>
          <span v-else class="tag ok">годен</span>
          <template v-if="editing === exit.tag">
            <button type="button" class="pill" :disabled="!!busy" @click="editing = ''">Отмена</button>
            <button type="button" class="pill w" :disabled="!!busy || !newHost.trim()" @click="check(exit, newHost.trim())">Проверить</button>
          </template>
          <template v-else>
            <button v-if="exit.skipped" type="button" class="pill" :disabled="!!busy" @click="check(exit)">Проверить снова</button>
            <button v-if="exit.skipped" type="button" class="pill" :disabled="!!busy" @click="edit(exit)">Заменить адрес</button>
            <button type="button" class="pill red" :disabled="!!busy" @click="put(exits.filter(e => e.tag !== exit.tag))">Убрать</button>
          </template>
        </div>
        <div v-if="exit.skipped && exit.problems?.length" class="al red">
          <b>Установка пойдёт без этого экзита</b>
          <span v-for="p in exit.problems" :key="p">{{ p }}</span>
        </div>
      </div>
      <p v-if="error" role="alert" class="wzbad">{{ error }}</p>
      <p v-if="exits.length && !exits.some(e => !e.skipped)" class="wzbad">Нужен хотя бы один годный экзит: без него установка не начнётся.</p>
    </div>

    <label class="fl2 wzsec">
      <span>Ваш открытый ключ ssh - обязателен. Деплой кладёт его в /root/.ssh/authorized_keys на всех серверах, вход по паролю закрыт везде</span>
      <textarea v-model="adminKey" class="area mono wzkey" placeholder="ssh-ed25519 AAAA… you@laptop" spellcheck="false" autocomplete="off" />
    </label>
  </section>
</template>
