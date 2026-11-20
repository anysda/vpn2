<script setup lang="ts">
// Ход задания менеджера: стадии по порядку, журнал текущей свёрнут, итог в конце, действия упавшего.
// Обычный Vue без Nuxt: опрос сам, через транспорт хоста; сбой связи - заметка и опрос дальше.
import { computed, onUnmounted, ref } from 'vue'
import { Refused, type ExitJob, type ExitTransport } from './transport'

const props = defineProps<{ transport: ExitTransport, job: ExitJob }>()
const emit = defineEmits<{ update: [job: ExitJob] }>()

const current = ref(props.job)
const lost = ref(false)
const acting = ref(false)
const actionError = ref('')

let timer: ReturnType<typeof setTimeout> | undefined
let alive = true
function schedule() {
  clearTimeout(timer)
  if (alive && current.value.state === 'running') timer = setTimeout(poll, 2000)
}
function take(job: ExitJob) {
  current.value = job
  emit('update', job)
  schedule()
}
async function poll() {
  try {
    const job = await props.transport.job(current.value.id)
    lost.value = false
    take(job)
  }
  catch {
    lost.value = true
    schedule()
  }
}
schedule()
onUnmounted(() => {
  alive = false
  clearTimeout(timer)
})

async function act(run: () => Promise<ExitJob>) {
  acting.value = true
  actionError.value = ''
  try {
    take(await run())
  }
  catch (e) {
    actionError.value = e instanceof Refused ? e.message : 'Нет связи с менеджером установки, повторите'
  }
  finally {
    acting.value = false
  }
}

const STAGES: Record<string, string> = {
  'preflight': 'Вход на серверы', '00-bootstrap': 'Подготовка сервера', '05-mgmt-mesh': 'Служебная сеть',
  '10-foreign': 'Службы экзита', '20-ru-router': 'Маршруты entry', '23-emergency': 'Аварийный доступ',
  '25-monitoring': 'Мониторинг', '30-frontend': 'Панель', '99-verify': 'Проверка', 'cleanup': 'Очистка сервера',
}
const STATE = { pending: 'ждёт', running: 'идёт…', done: 'готово', failed: 'ошибка' }
const COLOR = { pending: 'var(--t3)', running: 'var(--amber)', done: 'var(--green)', failed: 'var(--red)' }

const tag = computed(() => current.value.tag.toUpperCase())
const title = computed(() => ({ add: `Добавление экзита ${tag.value}`, remove: `Удаление экзита ${tag.value}`, install: 'Установка' })[current.value.kind])
const stateText = computed(() => ({ running: 'Идёт', done: 'Готово', failed: 'Ошибка', interrupted: 'Оборвалось', forgotten: 'Закрыто' })[current.value.state])
const running = computed(() => current.value.state === 'running')
const left = computed(() => current.value.state === 'done' ? current.value.result?.left ?? [] : [])
// Пропуск экзита - только у установки и только если хост его умеет.
const skippable = computed(() => props.transport.skip && current.value.actions.includes('skip')
  ? [...new Set(current.value.stages.flatMap(s => s.failed))].filter(t => t !== 'ru' && !current.value.skipped.includes(t)) : [])

const outcome = computed(() => {
  const { state, kind, result } = current.value
  if (state === 'done' && kind === 'remove') {
    if (result?.server === 'cleaned') return `Экзит ${tag.value} удалён, сервер очищен: наших служб и ключей на нём больше нет.`
    if (result?.server === 'forgotten') return `Экзит ${tag.value} удалён и забыт: сервер не ответил, на самом сервере ничего не менялось.`
    return `Экзит ${tag.value} удалён, но сервер очищен не до конца. Осталось:`
  }
  if (state === 'done') return kind === 'add' ? `Экзит ${tag.value} добавлен и работает.` : 'Установка завершена.'
  if (state === 'forgotten') return 'Задание закрыто.'
  if (state === 'running') return ''
  const why = state === 'interrupted' ? 'Задание оборвалось: перезагрузка entry или остановка службы.' : 'Задание остановилось с ошибкой.'
  const what = { add: 'Экзит не добавлен, состав экзитов прежний. «Повторить» продолжит добавление, «Забыть» уберёт то, что успело лечь.',
    remove: `Экзит ${tag.value} остаётся в составе. «Повторить» продолжит удаление, «Забыть» закроет задание.`,
    install: '«Повторить» продолжит установку с того же места.' }[kind]
  return `${why} ${what}`
})
</script>

<template>
  <section class="xjob" :aria-busy="running">
    <div class="sh">
      <b>{{ title }}</b>
      <span class="tag" :class="{ ok: current.state === 'done', badt: current.state === 'failed' || current.state === 'interrupted' }">{{ stateText }}</span>
    </div>
    <ol class="xst">
      <li v-for="s in current.stages" :key="s.name">
        <span class="dot" :style="{ background: COLOR[s.state] }" />
        <span>{{ STAGES[s.name] ?? s.name }}</span>
        <small>{{ STATE[s.state] }}<template v-if="s.failed.length"> · {{ s.failed.join(', ') }}</template></small>
      </li>
    </ol>
    <details v-if="current.log" class="xlog">
      <summary>Журнал{{ running ? ' текущего шага' : '' }}</summary>
      <pre>{{ current.log }}</pre>
    </details>
    <p v-if="lost" class="sub2" role="status">Нет связи с менеджером установки, повторяю…</p>
    <div v-if="current.error" role="alert" class="al red"><b>Ошибка</b>{{ current.error }}</div>
    <p v-if="outcome" class="mt" role="status">{{ outcome }}</p>
    <ul v-if="left.length" class="xleft">
      <li v-for="item in left" :key="item">{{ item }}</li>
    </ul>
    <div v-if="current.actions.length" class="ha xact">
      <button v-if="current.actions.includes('retry')" type="button" class="pill w" :disabled="acting" @click="act(() => transport.act(current.id, 'retry'))">
        Повторить
      </button>
      <button v-for="t in skippable" :key="t" type="button" class="pill" :disabled="acting" @click="act(() => transport.skip!(current.id, t))">
        Продолжить без {{ t.toUpperCase() }}
      </button>
      <button v-if="current.actions.includes('forget')" type="button" class="pill red" :disabled="acting" @click="act(() => transport.act(current.id, 'forget'))">
        Забыть
      </button>
    </div>
    <p v-if="actionError" role="alert" class="xerr">{{ actionError }}</p>
  </section>
</template>

<style scoped>
.xjob{display:flex;flex-direction:column;gap:8px;margin-top:14px;padding:14px 16px;border:1px solid var(--line);border-radius:14px}
.xst{list-style:none;margin:4px 0 0;padding:0;display:flex;flex-direction:column;gap:6px;font-size:13px;color:var(--t1)}
.xst li{display:flex;align-items:center;gap:8px}
.xst small{color:var(--t3);font-size:12px}
.xlog summary{cursor:pointer;color:var(--t2);font-size:12.5px}
.xlog pre{margin:8px 0 0;max-height:240px;overflow:auto;white-space:pre-wrap;word-break:break-all;font:12px/1.45 ui-monospace,monospace;color:var(--t2)}
.xleft{margin:0;padding-left:18px;color:var(--t2);font-size:13px}
.xact{flex-wrap:wrap}
.xerr{margin:0;color:var(--red-t);font-size:12.5px}
</style>
