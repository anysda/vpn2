<script setup lang="ts">
import { updateClient, type ClientDetail, type ClientTraffic, type Device } from '~/composables/useClients'
import { accessProtocols, accessStatus, protocolNames, type AccessOperation } from '#shared/access-operation'

const props = defineProps<{ clientId: number, traffic: ClientTraffic | null, exits: string[] }>()
const emit = defineEmits<{ changed: [], deleting: [id: number], gone: [id: number] }>()

const { toast, busy, act } = useAct()
const { canSend, reason: tgReason } = useTelegramStatus()
const ask = useAsk()
const d = ref<ClientDetail | null>(null)
const nameEl = ref<HTMLElement>()
let version = 0

// Опрос и сохранение перечитывают карточку, когда человек уже мог начать править
// соседнее поле: поле берёт значение сервера, только если в нём показанное до этого.
const expText = (c: ClientDetail) => formatRuDateUtc(c.expiresAt)
const limText = (c: ClientDetail) => c.deviceLimit === null ? '' : String(c.deviceLimit)
const expDraft = ref('')
const limDraft = ref('')

async function load() {
  const my = ++version
  const prev = d.value?.id === props.clientId ? d.value : null
  try {
    const next = await fetchClient(props.clientId)
    if (my !== version) return
    d.value = next
    if (!prev || expDraft.value === expText(prev)) expDraft.value = expText(next)
    if (!prev || limDraft.value === limText(prev)) limDraft.value = limText(next)
    await nextTick()
    const el = nameEl.value
    if (el && document.activeElement !== el && (!prev || el.textContent === prev.name)) el.textContent = next.name
  }
  catch (e) {
    if (my !== version) return
    // Клиента уже нет (удалён здесь, в другой вкладке или другим админом): страница выберет следующего.
    if ((e as { statusCode?: number }).statusCode === 404) emit('gone', props.clientId)
    else toast.add({ title: errText(e, 'Не удалось загрузить клиента'), color: 'error' })
  }
}
watch(() => props.clientId, load, { immediate: true })
usePoll(load, 3000)
onUnmounted(() => { version++ })

async function changed() {
  await load()
  emit('changed')
}

/** Правка поля; `quiet` - тост только с предупреждением сервера. */
async function save(patch: Parameters<typeof updateClient>[1], done: string, fail = 'Ошибка сохранения', quiet = false) {
  const id = d.value!.id
  const note = (r: { warning?: string }) => quiet && !r.warning ? undefined : doneToast(done, r.warning)
  if (!await act('save', () => updateClient(id, patch), note, fail)) return false
  if (d.value?.id === id) Object.assign(d.value, patch)
  emit('changed')
  return true
}

async function saveName() {
  const el = nameEl.value!
  const v = (el.textContent ?? '').trim()
  if (!v || v === d.value!.name) el.textContent = d.value!.name
  else if (await save({ name: v }, 'Имя сохранено')) el.textContent = v
}

async function saveExpiry() {
  const raw = expDraft.value.trim()
  const next = raw ? parseRuDateUtc(raw) : null
  if (raw && !next) {
    toast.add({ title: 'Срок действия — в формате ДД.ММ.ГГГГ', color: 'error' })
    expDraft.value = expText(d.value!)
    return
  }
  const ms = (iso: string | null) => iso ? new Date(iso).getTime() : null
  if (ms(next) !== ms(d.value!.expiresAt) && await save({ expiresAt: next }, 'Срок действия сохранён')) await load()
}

const minLimit = computed(() => Math.max(1, d.value?.devices.length ?? 0))
async function saveLimit(next: number | null, quiet = true) {
  limDraft.value = next === null ? '' : String(next)
  if (next === d.value!.deviceLimit) return
  if (!await save({ deviceLimit: next }, 'Лимит девайсов сохранён', 'Ошибка сохранения лимита', quiet)) limDraft.value = limText(d.value!)
}
function typedLimit() {
  const n = limDraft.value.trim() ? Number.parseInt(limDraft.value, 10) : null
  if (n === null || n >= minLimit.value) return saveLimit(n, false)
  toast.add({ title: `Лимит — не меньше ${minLimit.value}`, color: 'error' })
  limDraft.value = limText(d.value!)
}
const stepLimit = (by: number) => saveLimit(d.value!.deviceLimit === null ? minLimit.value : d.value!.deviceLimit + by)

async function saveExit(e: Event) {
  const el = e.target as HTMLSelectElement
  const tag = el.value || null
  if (!await save({ preferredExit: tag }, `Предпочитаемый экзит: ${tag ? tag.toUpperCase() : 'авто'}`, 'Ошибка')) el.value = d.value?.preferredExit ?? ''
}

async function toggleFreeze() {
  const next = !d.value!.frozenManual
  const done = { title: next ? 'Отключение выполняется' : 'Включение выполняется', color: 'info' as const }
  if (await act('freeze', () => updateClient(d.value!.id, { frozenManual: next }), done)) await changed()
}

/** Вопрос перед действием; после него тост «выполняется» и свежая карточка. */
function confirm(title: string, text: string, ok: string, fail: string, run: () => Promise<unknown>, started = '', danger = false) {
  ask.value = { title, text, ok, fail, danger, run: async () => {
    await run()
    if (started) toast.add({ title: started, color: 'info' })
    await changed()
  } }
}
const name = (s: string) => `«${s}»`

function askRemove(c: ClientDetail) {
  confirm('Удалить клиента?', `Удалить клиента ${name(c.name)} со всеми девайсами? Действие необратимо; доступ прекратится после подтверждения отключения.`,
    'Удалить', 'Ошибка удаления', async () => {
      await removeClient(c.id)
      emit('deleting', c.id)
    }, 'Отключение выполняется', true)
}

const UNSURE = 'Состояние не подтверждено'
const opTitle = computed(() => d.value?.operation ? accessStatus(d.value.operation) : UNSURE)
function protocolText(op: AccessOperation, p: typeof accessProtocols[number]) {
  const { state, error } = op.protocols[p]
  if (state === 'not_required') return 'Не требуется'
  return state === 'confirmed' && opTitle.value !== UNSURE ? 'Подтверждено' : error || UNSURE
}
const canRetry = (op: AccessOperation) => op.status !== 'superseded' && (op.status !== 'confirmed' || opTitle.value === UNSURE)
const retry = (op: AccessOperation) => act('retry', async () => {
  await $fetch<unknown>(`/api/access-operations/${op.id}/retry`, { method: 'POST' })
  await changed()
}, undefined, 'Не удалось повторить операцию')

const tg = computed(() => d.value?.tgLinked ? (d.value.tgUsername ? `@${d.value.tgUsername}` : '') : null)
const tag = computed(() => {
  const c = d.value!
  if (c.status === 'frozen') return { cls: '', text: opTitle.value }
  if (props.traffic?.online) return { cls: 'ok', text: 'В сети' }
  if (expiringInDays(c.expiresAt, c.status) !== null) return { cls: 'amb', text: 'Истекает' }
  return { cls: '', text: 'Не в сети' }
})
const rx = computed(() => props.traffic?.rxBytes ?? d.value?.rxTotal ?? 0)
const tx = computed(() => props.traffic?.txBytes ?? d.value?.txTotal ?? 0)

const KINDS = [['wg', 'WG'], ['ovpn', 'OVPN'], ['ike', 'IKEv2']] as const
const cfg = ref<{ device: Device, kind: 'wg' | 'ovpn' | 'ike' }>()
const cfgOpen = ref(false)
const newDev = reactive({ open: false, name: '' })
const limitReached = computed(() => d.value!.deviceLimit !== null && d.value!.devices.length >= d.value!.deviceLimit)
const used = (dev: Device) => dev.rxTotal + dev.txTotal

async function addDev() {
  const n = newDev.name.trim()
  if (n && await act('dev', async () => toast.add(doneToast(`Девайс «${n}» добавлен`, (await addDevice(d.value!.id, n)).warning)), undefined, 'Не удалось добавить девайс')) {
    newDev.open = false
    await changed()
  }
}
</script>

<template>
  <div v-if="d" class="cdet-in" :inert="d.id !== clientId">
    <div class="cdh">
      <span class="av">{{ initials(d.name) }}</span>
      <div class="min-w-0">
        <h2 ref="nameEl" contenteditable="plaintext-only" spellcheck="false" title="Имя клиента" @blur="saveName" @keydown.enter.prevent="nameEl?.blur()" />
        <small>{{ tg === null ? 'Telegram не привязан' : tg ? `Привязан к Telegram · ${tg}` : 'Привязан к Telegram' }}</small>
      </div>
      <span class="tag access-tag" :class="tag.cls">{{ tag.text }}</span>
    </div>

    <div class="kgrid">
      <div>
        <span>Истекает</span>
        <input
          v-model="expDraft" class="in" placeholder="бессрочно" title="Пусто = бессрочно" aria-label="Истекает" inputmode="numeric" maxlength="10"
          @input="expDraft = maskRuDate(expDraft)" @blur="saveExpiry" @keydown.enter.prevent="($event.target as HTMLElement).blur()">
      </div>
      <div><span>Создан</span><b>{{ dateRuLocal(d.createdAt) }}</b></div>
      <div><span>Суммарный трафик</span><b>{{ sizeBinRu(rx + tx) }}</b><em>↓ {{ sizeBinRu(rx) }} · ↑ {{ sizeBinRu(tx) }}</em></div>
      <div><span>Фильтр рекламы</span><b>{{ d.filterTraffic ? 'включён' : 'выключен' }}</b><em>меняется только при создании</em></div>
    </div>

    <div class="crw" role="status" aria-live="polite">
      <span>
        <small v-if="d.operation?.deviceId">Устройство #{{ d.operation.deviceId }} · </small>
        <b v-if="d.deleting">Удаляется · </b><b>{{ opTitle }}</b>
        <span v-for="p in d.operation ? accessProtocols : []" :key="p"><br>{{ protocolNames[p] }} · {{ protocolText(d.operation!, p) }}</span>
      </span>
      <button v-if="d.operation && canRetry(d.operation)" type="button" class="pill w" :disabled="busy.has('retry')" @click="retry(d.operation)">
        Повторить
      </button>
    </div>
    <div class="crw bt">
      <span>Бот · <b>{{ tg === null ? 'не привязан' : tg || 'привязан' }}</b></span>
      <div class="bta">
        <span :title="tgReason || undefined">
          <button
            type="button" class="pill w" :disabled="!canSend || busy.has('tg')"
            @click="act('tg', () => sendBotAccess(d!.id), { title: 'Приглашение отправлено в Telegram', color: 'success' }, 'Ошибка отправки')">
            Отправить доступ к боту
          </button>
        </span>
        <button
          v-if="d.tgLinked" type="button" class="pill red"
          @click="confirm('Отозвать доступ к боту?', `Отвязать Telegram клиента ${name(d.name)}? Он потеряет доступ к самообслуживанию в боте, а старая ссылка-приглашение перестанет работать. Конфиги VPN продолжат работать.`, 'Отозвать', 'Ошибка', async () => toast.add(doneToast('Доступ к боту отозван', (await unlinkClient(d!.id)).warning)))">
          Отозвать доступ к боту
        </button>
      </div>
    </div>
    <div class="crw">
      <span>Лимит девайсов<template v-if="d.deviceLimit === null"> · <b>лимит снят (∞)</b></template></span>
      <div class="stp">
        <button type="button" aria-label="Меньше" :disabled="d.deviceLimit !== null && d.deviceLimit <= minLimit" @click="stepLimit(-1)">
          −
        </button>
        <input
          v-model="limDraft" class="in" placeholder="∞" inputmode="numeric" aria-label="Лимит девайсов"
          @change="typedLimit" @keydown.enter.prevent="($event.target as HTMLElement).blur()">
        <button type="button" aria-label="Больше" @click="stepLimit(1)">
          +
        </button>
      </div>
      <button
        type="button" class="pill" :class="{ on: d.deviceLimit === null }" title="Безлимитно" aria-label="Безлимитно" :aria-pressed="d.deviceLimit === null"
        @click="saveLimit(d.deviceLimit === null ? Math.max(d.devices.length, 3) : null)">
        ∞
      </button>
    </div>
    <div class="crw">
      <span>Предпочитаемый экзит</span>
      <span class="sel">
        <select :value="d.preferredExit ?? ''" aria-label="Предпочитаемый экзит" @change="saveExit">
          <option value="">Авто</option>
          <option v-for="x in exits" :key="x" :value="x">{{ x.toUpperCase() }}</option>
        </select>
        <svg class="i" viewBox="0 0 24 24"><path d="M7 10l5 5 5-5" /></svg>
      </span>
    </div>

    <div class="sub">
      <h3>Девайсы {{ d.devices.length }} / {{ d.deviceLimit ?? '∞' }}</h3>
      <span :title="limitReached ? 'Достигнут лимит девайсов' : undefined">
        <button type="button" class="pill" :disabled="limitReached" @click="Object.assign(newDev, { open: true, name: '' })">+ девайс</button>
      </span>
    </div>
    <div class="devl scroll">
      <div v-for="dev in d.devices" :key="dev.id" class="dev" data-testid="device-row">
        <div>
          <b>{{ dev.name }}</b>
          <small :class="{ tr: used(dev) }" :title="used(dev) ? `↓ ${sizeBinRu(dev.rxTotal)} ↑ ${sizeBinRu(dev.txTotal)}` : undefined">
            {{ used(dev) ? sizeBinRu(used(dev)) : 'нет трафика' }}
          </small>
        </div>
        <div class="kb">
          <button v-for="[kind, label] in KINDS" :key="kind" type="button" @click="cfg = { device: dev, kind }; cfgOpen = true">
            {{ label }}
          </button>
          <button
            type="button" class="ic" title="Перевыпустить ключи девайса" aria-label="Перевыпустить ключи девайса"
            @click="confirm('Перевыпустить ключи девайса?', `Перевыпустить ключи девайса ${name(dev.name)}? После применения старые конфиги этого девайса перестанут работать.`, 'Перевыпустить', 'Ошибка перевыпуска', () => reissueDevice(d!.id, dev.id), 'Перевыпуск выполняется')">
            <svg class="i" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5" /></svg>
          </button>
          <button
            type="button" class="ic" title="Удалить девайс" aria-label="Удалить девайс"
            @click="confirm('Удалить девайс?', `Удалить девайс ${name(dev.name)}? Действие необратимо; конфиги перестанут работать после подтверждения отключения.`, 'Удалить', 'Ошибка удаления', () => removeDevice(d!.id, dev.id), 'Отключение выполняется', true)">
            <svg class="i" viewBox="0 0 24 24"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13" /></svg>
          </button>
        </div>
      </div>
      <div v-if="!d.devices.length" class="dev nod">
        Пока нет девайсов — добавь первый.
      </div>
    </div>
    <div class="acts">
      <button
        type="button" class="pill"
        @click="confirm('Перевыпустить все ключи?', `Перевыпустить ключи всех девайсов клиента ${name(d.name)}? После применения старые конфиги перестанут работать — нужно будет раздать новые.`, 'Перевыпустить', 'Ошибка перевыпуска', () => reissueClient(d!.id), 'Перевыпуск выполняется')">
        Перевыпустить все ключи
      </button>
      <button type="button" class="pill" :disabled="busy.has('freeze')" @click="toggleFreeze">
        {{ d.frozenManual ? 'Разморозить' : 'Заморозить' }}
      </button>
      <button type="button" class="pill red" @click="askRemove(d)">
        Удалить клиента
      </button>
    </div>

    <PanelDialog v-model:open="newDev.open" title="Новый девайс" ok="Добавить" :busy="!newDev.name.trim() || busy.has('dev')" @ok="addDev">
      <label class="fl2"><span>Название девайса</span><input v-model="newDev.name" class="in" placeholder="напр. iPhone" @keydown.enter="addDev"></label>
    </PanelDialog>
    <ClientsDeviceConfig v-if="cfg" v-model:open="cfgOpen" :client-id="d.id" :device="cfg.device" :kind="cfg.kind" />
  </div>
</template>
