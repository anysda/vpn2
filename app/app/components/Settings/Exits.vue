<script setup lang="ts">
import ExitAdd from '~~/ui/ExitAdd.vue'
import ExitJobView from '~~/ui/ExitJob.vue'
import { failedStatus } from '#shared/error-text'
import { OPEN_STATES, Refused, type ExitJob, type ExitTransport } from '~~/ui/transport'

type Status = Api<'/api/exits'>
const { data: status, error, refresh } = usePolledFetch('/api/exits', 3000, 10_000, null as Status | null)
const ask = useAsk()

// Отказ менеджера (409) - его готовая фраза, прочее - нет связи: экран опрашивает дальше.
async function call<T>(request: Promise<T>): Promise<T> {
  try {
    return await request
  }
  catch (e) {
    throw failedStatus(e) === 409 ? new Refused(errText(e)) : e
  }
}
const transport: ExitTransport = {
  key: async () => status.value?.key || (await call($fetch<Status>('/api/exits'))).key,
  check: async (tag, host) => (await call($fetch<{ problems: string[] }>('/api/exits/check', { method: 'POST', body: { tag, host } }))).problems,
  add: (tag, host) => call($fetch<ExitJob>('/api/exits', { method: 'POST', body: { tag, host } })),
  job: id => call($fetch<ExitJob>(`/api/exits/jobs/${id}`)),
  act: (id, action) => call($fetch<ExitJob>(`/api/exits/jobs/${id}/${action}`, { method: 'POST' })),
}

const adding = ref(false)
// Задания, что шли при открытой странице: их итог виден, пока его не скрыли.
const seen = ref(new Set<number>())
const hidden = ref(new Set<number>())
const exits = computed(() => (status.value?.nodes ?? []).filter(n => n.role === 'exit'))
const job = computed(() => status.value?.job as ExitJob | null | undefined)
const open = computed(() => !!job.value && OPEN_STATES.includes(job.value.state))
watch(job, (j) => { if (j && open.value) seen.value.add(j.id) }, { immediate: true })
const shown = computed(() => job.value && !adding.value && !hidden.value.has(job.value.id) && (open.value || seen.value.has(job.value.id)) ? job.value : null)

const blocked = computed(() => {
  if (job.value?.state === 'running') return 'Идёт задание менеджера установки: добавлять и удалять экзиты можно после его конца'
  if (open.value) return 'Прошлое задание не закрыто: повторите его или забудьте'
  if (status.value?.deploy === 'busy') return 'Идёт ./deploy.sh в терминале: добавлять и удалять экзиты можно после его конца'
  return ''
})
const removeBlocked = computed(() => blocked.value || (status.value && exits.value.length <= 1 ?'Последний экзит удалить нельзя: без экзита VPN не работает' : ''))
const MONITORING = { up: ['ok', 'В работе'], down: ['badt', 'Нет связи'], unknown: ['', 'Нет данных'] } as const

function onJob(next: ExitJob) {
  seen.value.add(next.id)
  if (next.state !== 'running') refresh()
}

// Закрытый экран добавления уносит свой законченный итог; идущее, упавшее и прерванное остаются с «Повторить/Забыть».
let added: ExitJob | null = null
function onAdd(next: ExitJob) {
  added = next
  onJob(next)
}
function close() {
  if (added && !OPEN_STATES.includes(added.state)) hidden.value.add(added.id)
  added = null
  adding.value = false
  refresh()
}

function remove(tag: string) {
  ask.value = {
    title: `Удалить экзит ${exitLabel(tag)}?`,
    text: 'Экзит уйдёт из маршрутов, мониторинга и config.yaml; клиенты, закреплённые за ним, перейдут на авто. '
      + 'Если сервер отвечает, наши службы и ключи на нём удаляются; если нет, экзит просто забывается.',
    ok: 'Удалить',
    danger: true,
    fail: 'Экзит не удалён',
    run: async () => {
      onJob(await $fetch<ExitJob>(`/api/exits/${tag}`, { method: 'DELETE' }))
      await refresh()
    },
  }
}
</script>

<template>
  <section class="card sc sexits">
    <div class="sh">
      <h3>Экзиты</h3>
      <div class="ha">
        <button v-if="!adding" type="button" class="pill" :disabled="!status || !!blocked" @click="adding = true">
          Добавить экзит
        </button>
        <button v-else type="button" class="pill" @click="close">Закрыть</button>
      </div>
    </div>
    <p v-if="error && status" class="sub2" role="status">Нет связи с менеджером установки, повторяю…</p>
    <div v-else-if="error" role="alert" class="al red"><b>Список экзитов не загрузился</b>{{ errText(error) }}</div>
    <div v-for="n in exits" :key="n.tag" class="fld xrow">
      <span>{{ exitLabel(n.tag) }}</span>
      <code class="mono">{{ n.host }}</code>
      <span class="tag" :class="MONITORING[n.monitoring][0]">{{ MONITORING[n.monitoring][1] }}</span>
      <button type="button" class="pill red" :disabled="!!removeBlocked" @click="remove(n.tag)">Удалить</button>
    </div>
    <p v-if="removeBlocked" class="sub2 xwhy" role="note">{{ removeBlocked }}</p>
    <ExitJobView v-if="shown" :key="shown.id" :transport="transport" :job="shown" @update="onJob" />
    <div v-if="shown && !open" class="fld">
      <span /><code />
      <button type="button" class="pill" @click="hidden.add(shown.id)">Скрыть</button>
    </div>
    <ExitAdd v-if="adding" :transport="transport" @job="onAdd" @close="close" />
  </section>
</template>

<style scoped>
.sexits{grid-column:1/-1;min-height:0}
.xrow{margin-top:0;flex-wrap:wrap}
.xrow code{min-width:0;overflow-wrap:anywhere}
.xwhy{margin-top:10px}
</style>
