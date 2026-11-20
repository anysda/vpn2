<script setup lang="ts">
// Новый экзит: ключ оркестратора, адрес и тег, проверка менеджером, установка и её ход.
// Один экран для настроек панели и мастера установки: обычный Vue, менеджер - через транспорт хоста.
import { computed, onMounted, ref } from 'vue'
import ExitJobView from './ExitJob.vue'
import { Refused, type ExitJob, type ExitTransport } from './transport'

const props = defineProps<{ transport: ExitTransport }>()
const emit = defineEmits<{ job: [job: ExitJob], close: [] }>()

const NO_LINK = 'Нет связи с менеджером установки, повторите'
const key = ref('')
const host = ref('')
const tag = ref('')
const checking = ref(false)
const checked = ref<{ tag: string, host: string, problems: string[] } | null>(null)
const checkError = ref('')
const starting = ref(false)
const startError = ref('')
const job = ref<ExitJob | null>(null)
const copied = ref(false)

onMounted(async () => {
  key.value = await props.transport.key().catch(() => '')
})

const value = () => ({ tag: tag.value.trim(), host: host.value.trim() })
// Проверка годится только для того, что сейчас в полях: правка адреса - проверить заново.
const fresh = computed(() => checked.value?.tag === value().tag && checked.value?.host === value().host)
const ready = computed(() => fresh.value && !checked.value!.problems.length)
const finished = computed(() => !!job.value && job.value.state !== 'running')

async function check() {
  const { tag: t, host: h } = value()
  checking.value = true
  checkError.value = ''
  try {
    checked.value = { tag: t, host: h, problems: await props.transport.check(t, h) }
  }
  catch (e) {
    checked.value = null
    checkError.value = e instanceof Refused ? e.message : NO_LINK
  }
  finally {
    checking.value = false
  }
}

async function install() {
  starting.value = true
  startError.value = ''
  try {
    update(await props.transport.add(value().tag, value().host))
  }
  catch (e) {
    startError.value = e instanceof Refused ? e.message : NO_LINK
  }
  finally {
    starting.value = false
  }
}

function reset() {
  host.value = ''
  tag.value = ''
  checked.value = null
}

function update(next: ExitJob) {
  job.value = next
  emit('job', next)
}

async function copy() {
  copied.value = await navigator.clipboard?.writeText(key.value).then(() => true, () => false) ?? false
}
</script>

<template>
  <section class="xadd">
    <template v-if="!job">
      <p class="mt">
        Сервер экзита: Ubuntu 26.04 и вход root по ключу. Добавьте на нём в <code>/root/.ssh/authorized_keys</code> строку ключа оркестратора:
      </p>
      <div class="xkey">
        <code class="mono" data-testid="orchestrator-key">{{ key || 'ключ загружается…' }}</code>
        <button type="button" class="pill" :disabled="!key" @click="copy">{{ copied ? 'Скопировано' : 'Копировать' }}</button>
      </div>
      <fieldset :disabled="starting" class="xform">
        <label class="fld"><span>Адрес</span><input v-model="host" class="in mono" placeholder="root@203.0.113.10" autocomplete="off" spellcheck="false"></label>
        <label class="fld">
          <span>Тег</span><input v-model="tag" class="in mono" placeholder="de" autocomplete="off" spellcheck="false">
        </label>
        <div class="fld">
          <span>Проверка</span>
          <code role="status">
            <template v-if="checking">проверяю…</template>
            <span v-else-if="checkError" class="xbad">{{ checkError }}</span>
            <span v-else-if="ready" class="xgood">сервер годен: вход по ключу есть, Ubuntu 26.04</span>
            <template v-else-if="fresh">есть замечания: исправьте и проверьте снова</template>
            <template v-else-if="checked">данные изменились: проверьте снова</template>
            <template v-else>ещё не проверяли</template>
          </code>
          <button type="button" class="pill" :disabled="checking || !tag.trim() || !host.trim()" @click="check">Проверить</button>
        </div>
        <div v-if="fresh && checked!.problems.length" role="alert" class="al red">
          <b>Сервер пока не годится</b>
          <span v-for="p in checked!.problems" :key="p">{{ p }}</span>
        </div>
        <!-- Мастер установки ставит экзит не сразу, а в черновик: свой ряд кнопок вместо «Установить». -->
        <slot name="submit" :value="value()" :ready="ready" :problems="fresh ? checked!.problems : null" :reset="reset">
          <div class="fld">
            <span /><code />
            <button type="button" class="pill w" :disabled="!ready || starting" @click="install">Установить</button>
          </div>
        </slot>
      </fieldset>
      <p v-if="startError" role="alert" class="xbad">{{ startError }}</p>
    </template>
    <template v-else>
      <ExitJobView :transport="transport" :job="job" @update="update" />
      <div v-if="finished && job.state !== 'failed' && job.state !== 'interrupted'" class="fld">
        <span /><code />
        <button type="button" class="pill w" @click="emit('close')">Готово</button>
      </div>
    </template>
  </section>
</template>

<style scoped>
.xkey{display:flex;align-items:center;gap:12px;margin:10px 0 4px}
.xkey code{flex:1;min-width:0;overflow-wrap:anywhere;font-size:12px;color:var(--t1);user-select:all}
.xform{border:0;margin:0;padding:0;min-width:0}
.xform .in{flex:1;min-width:0}
.xgood{color:var(--green)}
.xbad{color:var(--red-t);overflow-wrap:anywhere}
p.xbad{margin:8px 0 0;font-size:12.5px}
.al span{overflow-wrap:anywhere}
</style>
