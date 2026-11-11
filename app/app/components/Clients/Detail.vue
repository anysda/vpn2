<script setup lang="ts">
import type { ClientDetail, ClientTraffic } from '~/composables/useClients'
import { expiringInDays, initials } from '~/utils/clients-view'
import { dateRuLocal, formatRuDateUtc, parseRuDateUtc, sizeBinRu } from '~/utils/format'
import { maskRuDate } from '~/utils/ru-date-mask'
import { accessProtocols, accessStatus, protocolNames } from '#shared/access-operation'
import type { AccessOperation } from '#shared/access-operation'
import type { Ask } from './Ask.vue'

const props = defineProps<{ clientId: number, traffic: ClientTraffic | null, exits: string[] }>()
const emit = defineEmits<{
  changed: []
  deleting: [id: number, accepted: Promise<{ operation: AccessOperation }>]
  deleted: [id: number]
  gone: [id: number]
}>()

const { toast, busy, act } = useAct()
const { canSend: canSendTg, reason: tgReason } = useTelegramStatus()

const root = ref<HTMLElement>()
const nameEl = ref<HTMLElement>()
const detail = ref<ClientDetail | null>(null)
let loadVersion = 0
// Выбран другой клиент, а на экране ещё прежний: его поля не правятся,
// иначе введённое уходило бы прежнему клиенту.
const stale = computed(() => detail.value !== null && detail.value.id !== props.clientId)

async function load(animate = false) {
  const version = ++loadVersion
  try {
    const previous = detail.value
    if (previous?.id === props.clientId && previous.deleting && previous.operation) {
      const operation = await $fetch<AccessOperation>(`/api/access-operations/${previous.operation.id}`)
      if (version !== loadVersion) return
      if (operation.status === 'confirmed') return emit('deleted', props.clientId)
      previous.operation = operation
      return
    }
    const d = await fetchClient(props.clientId)
    if (version !== loadVersion) return
    detail.value = d
    syncForm(previous)
    if (animate) {
      nextTick(() => anim(root.value, [
        { opacity: 0.2, filter: 'blur(5px)', transform: 'translateY(4px)' },
        { opacity: 1, filter: 'blur(0)', transform: 'none' },
      ], { duration: 320 }))
    }
  }
  catch (e) {
    if (version !== loadVersion) return
    // Клиента уже нет (удалён в другой вкладке или другим админом) — не ошибка:
    // страница молча выберет следующего.
    if ((e as { statusCode?: number }).statusCode === 404) return emit('gone', props.clientId)
    toast.add({ title: errText(e, 'Не удалось загрузить клиента'), color: 'error' })
  }
}

watch(() => props.clientId, () => load(detail.value !== null), { immediate: true })
useEvery(load, 3000)
onUnmounted(() => { loadVersion++ })

async function changed() {
  await load()
  emit('changed')
}

// --- Поля: имя в заголовке, срок, лимит ------------------------------------
const expDraft = ref('')
const limDraft = ref('')
const expText = (d: ClientDetail) => formatRuDateUtc(d.expiresAt)
const limText = (d: ClientDetail) => d.deviceLimit === null ? '' : String(d.deviceLimit)

// Карточка перезагружается опросом и после сохранения, когда человек уже мог
// начать править соседнее поле (или имя не сохранилось из-за 409): поле берёт
// значение сервера, только если в нём то, что было показано до перезагрузки.
// Другой клиент — поля заново.
function syncForm(prev: ClientDetail | null) {
  const d = detail.value!
  const same = prev?.id === d.id ? prev : null
  if (!same || expDraft.value === expText(same)) expDraft.value = expText(d)
  if (!same || limDraft.value === limText(same)) limDraft.value = limText(d)
  nextTick(() => {
    const el = nameEl.value
    if (!el || document.activeElement === el) return
    if (!same || !el.textContent || el.textContent === same.name) el.textContent = d.name
  })
}

/** Правка клиента: тост done (quiet — только с предупреждением сервера), свежий список. */
async function save(patch: Parameters<typeof updateClient>[1], done: string, fail = 'Ошибка сохранения', quiet = false) {
  const { id } = detail.value!
  const note = (res: { warning?: string }) => quiet && !res.warning ? undefined : doneToast(done, res.warning)
  if (!await act('save', () => updateClient(id, patch), note, fail)) return false
  if (detail.value?.id === id) Object.assign(detail.value, patch)
  emit('changed')
  return true
}

async function saveName() {
  const d = detail.value!
  const el = nameEl.value!
  const v = (el.textContent ?? '').trim()
  if (!v || v === d.name) el.textContent = d.name
  else if (await save({ name: v }, 'Имя сохранено')) el.textContent = v
}

function onExpInput(e: Event) {
  const el = e.target as HTMLInputElement
  expDraft.value = el.value = maskRuDate(el.value)
}

async function saveExpiry() {
  const d = detail.value!
  const raw = expDraft.value.trim()
  const next = raw ? parseRuDateUtc(raw) : null
  if (raw && !next) {
    toast.add({ title: 'Срок действия — в формате ДД.ММ.ГГГГ', color: 'error' })
    expDraft.value = expText(d)
    return
  }
  const ms = (iso: string | null) => iso ? new Date(iso).getTime() : null
  if (ms(next) !== ms(d.expiresAt) && await save({ expiresAt: next }, 'Срок действия сохранён')) await load()
}

const minLimit = computed(() => Math.max(1, detail.value?.devices.length ?? 0))

async function saveLimit(next: number | null, quiet = true) {
  const d = detail.value!
  limDraft.value = next === null ? '' : String(next)
  if (next === d.deviceLimit) return
  if (!await save({ deviceLimit: next }, 'Лимит девайсов сохранён', 'Ошибка сохранения лимита', quiet)) limDraft.value = limText(d)
}

function onLimitChange() {
  const v = limDraft.value.trim()
  const n = v ? Number.parseInt(v, 10) : null
  if (n === null || n >= minLimit.value) return saveLimit(n, false)
  toast.add({ title: `Лимит — не меньше ${minLimit.value}`, color: 'error' })
  limDraft.value = limText(detail.value!)
}

/** «−» и «+»; от снятого лимита — к наименьшему. */
function stepLimit(by: number) {
  const lim = detail.value!.deviceLimit
  return saveLimit(lim === null ? minLimit.value : lim + by)
}

function toggleUnlimited() {
  const d = detail.value!
  return saveLimit(d.deviceLimit === null ? Math.max(d.devices.length, 3) : null)
}

async function saveExit(e: Event) {
  const el = e.target as HTMLSelectElement
  const tag = el.value || null
  const done = `Предпочитаемый экзит: ${tag ? tag.toUpperCase() : 'авто'}`
  if (!await save({ preferredExit: tag }, done, 'Ошибка')) el.value = detail.value?.preferredExit ?? ''
}

// --- Действия --------------------------------------------------------------
function sendAccess() {
  const done = { title: 'Приглашение отправлено в Telegram', color: 'success' as const }
  return act('tg', () => sendBotAccess(detail.value!.id), done, 'Ошибка отправки')
}

async function toggleFreeze() {
  const d = detail.value!
  const next = !d.frozenManual
  const done = { title: next ? 'Отключение выполняется' : 'Включение выполняется', color: 'info' as const }
  if (await act('freeze', () => updateClient(d.id, { frozenManual: next }), done)) await changed()
}

const asker = ref<{ confirm: (a: Ask) => void }>()
/** Вопрос перед действием; после него тост «выполняется» и свежая карточка. */
function ask(a: Omit<Ask, 'run'>, run: () => Promise<unknown>, started = '') {
  asker.value?.confirm({
    ...a,
    run: async () => {
      await run()
      if (started) toast.add({ title: started, color: 'info' })
      await changed()
    },
  })
}

function askUnlink(d: ClientDetail) {
  ask({
    title: 'Отозвать доступ к боту?',
    pre: 'Отвязать Telegram клиента ',
    name: d.name,
    post: '? Он потеряет доступ к самообслуживанию в боте, а старая ссылка-приглашение перестанет работать. Конфиги VPN продолжат работать.',
    act: 'Отозвать',
    fail: 'Ошибка',
  }, async () => toast.add(doneToast('Доступ к боту отозван', (await unlinkClient(d.id)).warning)))
}

function askReissueAll(d: ClientDetail) {
  ask({
    title: 'Перевыпустить все ключи?',
    pre: 'Перевыпустить ключи всех девайсов клиента ',
    name: d.name,
    post: '? После применения старые конфиги перестанут работать — нужно будет раздать новые.',
    act: 'Перевыпустить',
    fail: 'Ошибка перевыпуска',
  }, () => reissueClient(d.id), 'Перевыпуск выполняется')
}

function askRemove(d: ClientDetail) {
  ask({
    title: 'Удалить клиента?',
    pre: 'Удалить клиента ',
    name: d.name,
    post: ' со всеми девайсами? Действие необратимо; доступ прекратится после подтверждения отключения.',
    act: 'Удалить',
    danger: true,
    fail: 'Ошибка удаления',
  }, async () => {
    const deleting = removeClient(d.id)
    emit('deleting', d.id, deleting)
    const { operation } = await deleting
    // Пока окно было открыто, опрос мог подменить карточку новым объектом.
    if (detail.value?.id === d.id) Object.assign(detail.value, { operation, deleting: true })
  }, 'Отключение выполняется')
}

// --- Состояние доступа -----------------------------------------------------
const UNSURE = 'Состояние не подтверждено'
const opTitle = computed(() => detail.value?.operation ? accessStatus(detail.value.operation) : UNSURE)

function protocolText(op: AccessOperation, p: typeof accessProtocols[number]) {
  const { state, error } = op.protocols[p]
  if (state === 'not_required') return 'Не требуется'
  return state === 'confirmed' && opTitle.value !== UNSURE ? 'Подтверждено' : error || UNSURE
}

async function retry(op: AccessOperation) {
  busy.value.add('retry')
  await $fetch(`/api/access-operations/${op.id}/retry`, { method: 'POST' })
    .then(changed, () => toast.add({ title: 'Не удалось повторить операцию', color: 'error' }))
  busy.value.delete('retry')
}

// --- Девайсы и показатели --------------------------------------------------
/** Ник в Telegram: null — не привязан, пусто — привязан без ника. */
const tg = computed(() => {
  const d = detail.value
  return d?.tgLinked ? (d.tgUsername ? `@${d.tgUsername}` : '') : null
})

const tag = computed(() => {
  const d = detail.value
  if (!d) return null
  if (d.status === 'frozen') return { cls: '', text: opTitle.value }
  if (props.traffic?.online) return { cls: 'ok', text: 'В сети' }
  if (expiringInDays(d.expiresAt, d.status) !== null) return { cls: 'amb', text: 'Истекает' }
  return { cls: '', text: 'Не в сети' }
})

const rx = computed(() => props.traffic?.rxBytes ?? detail.value?.rxTotal ?? 0)
const tx = computed(() => props.traffic?.txBytes ?? detail.value?.txTotal ?? 0)
</script>

<template>
  <div ref="root" class="cdet-in" :inert="stale" :aria-busy="stale">
    <template v-if="detail">
      <div class="cdh">
        <span class="av">{{ initials(detail.name) }}</span>
        <div style="min-width:0">
          <h2
            ref="nameEl"
            contenteditable="plaintext-only"
            spellcheck="false"
            title="Имя клиента"
            @blur="saveName"
            @keydown.enter.prevent="nameEl?.blur()" />
          <small>{{ tg === null ? 'Telegram не привязан' : tg ? `Привязан к Telegram · ${tg}` : 'Привязан к Telegram' }}</small>
        </div>
        <span v-if="tag" class="tag access-tag" :class="tag.cls">{{ tag.text }}</span>
      </div>

      <div class="kgrid">
        <div>
          <span>Истекает</span>
          <input
            :value="expDraft"
            class="in"
            placeholder="бессрочно"
            title="Пусто = бессрочно"
            aria-label="Истекает"
            inputmode="numeric"
            maxlength="10"
            @input="onExpInput"
            @blur="saveExpiry"
            @keydown.enter.prevent="($event.target as HTMLInputElement).blur()">
        </div>
        <div><span>Создан</span><b>{{ dateRuLocal(detail.createdAt) }}</b></div>
        <div>
          <span>Суммарный трафик</span><b>{{ sizeBinRu(rx + tx) }}</b>
          <em>↓ {{ sizeBinRu(rx) }} · ↑ {{ sizeBinRu(tx) }}</em>
        </div>
        <div>
          <span>Фильтр рекламы</span><b>{{ detail.filterTraffic ? 'включён' : 'выключен' }}</b>
          <em>меняется только при создании</em>
        </div>
      </div>

      <div class="crw" role="status" aria-live="polite">
        <span>
          <small v-if="detail.operation?.deviceId">Устройство #{{ detail.operation.deviceId }} · </small>
          <b v-if="detail.deleting">Удаляется · </b><b>{{ opTitle }}</b>
          <span v-for="p in detail.operation ? accessProtocols : []" :key="p">
            <br>{{ protocolNames[p] }} · {{ protocolText(detail.operation!, p) }}
          </span>
        </span>
        <button
          v-if="detail.operation && detail.operation.status !== 'superseded'
            && (detail.operation.status !== 'confirmed' || opTitle === UNSURE)"
          type="button"
          class="pill w"
          :disabled="busy.has('retry')"
          @click="retry(detail.operation)">
          Повторить
        </button>
      </div>
      <div class="crw bt">
        <span>Бот · <b>{{ tg === null ? 'не привязан' : tg || 'привязан' }}</b></span>
        <div class="bta">
          <span :title="canSendTg ? undefined : tgReason">
            <button type="button" class="pill w" :disabled="!canSendTg || busy.has('tg')" @click="sendAccess">
              Отправить доступ к боту
            </button>
          </span>
          <button v-if="detail.tgLinked" type="button" class="pill red" @click="askUnlink(detail)">
            Отозвать доступ к боту
          </button>
        </div>
      </div>
      <div class="crw">
        <span>Лимит девайсов<template v-if="detail.deviceLimit === null"> · <b>лимит снят (∞)</b></template></span>
        <div class="stp">
          <button
            type="button"
            aria-label="Меньше"
            :disabled="detail.deviceLimit !== null && detail.deviceLimit <= minLimit"
            @click="stepLimit(-1)">
            −
          </button>
          <input
            v-model="limDraft"
            class="in"
            placeholder="∞"
            inputmode="numeric"
            aria-label="Лимит девайсов"
            @change="onLimitChange"
            @keydown.enter.prevent="($event.target as HTMLInputElement).blur()">
          <button type="button" aria-label="Больше" @click="stepLimit(1)">
            +
          </button>
        </div>
        <button
          type="button"
          class="pill"
          :class="{ on: detail.deviceLimit === null }"
          title="Безлимитно"
          aria-label="Безлимитно"
          :aria-pressed="detail.deviceLimit === null"
          @click="toggleUnlimited">
          ∞
        </button>
      </div>
      <div class="crw">
        <span>Предпочитаемый экзит</span>
        <span class="sel">
          <select :value="detail.preferredExit ?? ''" aria-label="Предпочитаемый экзит" @change="saveExit">
            <option value="">Авто</option>
            <option v-for="x in exits" :key="x" :value="x">{{ x.toUpperCase() }}</option>
          </select>
          <svg class="i" viewBox="0 0 24 24"><path d="M7 10l5 5 5-5" /></svg>
        </span>
      </div>

      <ClientsDevices :detail="detail" :ask="ask" @added="changed" />
      <div class="acts">
        <button type="button" class="pill" @click="askReissueAll(detail)">
          Перевыпустить все ключи
        </button>
        <button type="button" class="pill" :disabled="busy.has('freeze')" @click="toggleFreeze">
          {{ detail.frozenManual ? 'Разморозить' : 'Заморозить' }}
        </button>
        <button type="button" class="pill red" @click="askRemove(detail)">
          Удалить клиента
        </button>
      </div>
    </template>

    <ClientsAsk ref="asker" />
  </div>
</template>
