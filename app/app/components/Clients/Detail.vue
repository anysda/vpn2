<script setup lang="ts">
import type { ClientDetail, ClientTraffic, Device } from '~/composables/useClients'
import { formatRuDate, parseRuDate, useClientDetail, useClientDevices, useClients } from '~/composables/useClients'
import { useTelegramStatus } from '~/composables/useTelegramStatus'
import { anim, expiringInDays, initials, sizeRu } from '~/utils/clients-view'
import { maskRuDate } from '~/utils/ru-date-mask'
import type { ConfigKind } from './DeviceConfig.vue'

const props = defineProps<{ clientId: number, traffic: ClientTraffic | null, exits: string[] }>()
const emit = defineEmits<{ changed: [], deleted: [], gone: [id: number] }>()

const { fetchDetail } = useClientDetail()
const { update, remove, reissueAll, sendPasswordToTg, unlink } = useClients()
const { removeDevice, reissueDevice } = useClientDevices()
const { canSend: canSendTg, reason: tgReason } = useTelegramStatus()
const toast = useToast()
const mobile = useMobile()

const root = ref<HTMLElement>()
const nameEl = ref<HTMLElement>()
const detail = ref<ClientDetail | null>(null)
let loadVersion = 0

function errTitle(e: unknown, fallback: string) {
  return (e as { statusMessage?: string }).statusMessage ?? fallback
}

/** Клиента уже нет: удалён в другой вкладке или другим админом. */
function isGone(e: unknown) {
  return (e as { statusCode?: number }).statusCode === 404
}

async function load(animate = false) {
  const version = ++loadVersion
  try {
    const d = await fetchDetail(props.clientId)
    if (version !== loadVersion) return
    detail.value = d
    syncForm()
    if (animate) {
      nextTick(() => anim(root.value, [
        { opacity: 0.2, filter: 'blur(5px)', transform: 'translateY(4px)' },
        { opacity: 1, filter: 'blur(0)', transform: 'none' },
      ], { duration: 320 }))
    }
  }
  catch (e) {
    if (version !== loadVersion) return
    // Пропавший клиент — не ошибка: страница молча выберет следующего.
    if (isGone(e)) return emit('gone', props.clientId)
    toast.add({ title: errTitle(e, 'Не удалось загрузить клиента'), color: 'error' })
  }
}

watch(() => props.clientId, () => load(detail.value !== null), { immediate: true })
onUnmounted(() => { loadVersion++ })

async function changed() {
  await load()
  emit('changed')
}

// --- Имя: правится прямо в заголовке ---------------------------------------
function syncName() {
  const el = nameEl.value
  if (el && detail.value && document.activeElement !== el) el.textContent = detail.value.name
}

async function saveName() {
  const d = detail.value
  const el = nameEl.value
  if (!d || !el) return
  const v = (el.textContent ?? '').trim()
  if (!v || v === d.name) {
    el.textContent = d.name
    return
  }
  try {
    await update(d.id, { name: v })
    d.name = v
    el.textContent = v
    toast.add({ title: 'Имя сохранено', color: 'success' })
    emit('changed')
  }
  catch (e) {
    toast.add({ title: errTitle(e, 'Ошибка сохранения'), color: 'error' })
  }
}

// --- Срок действия ---------------------------------------------------------
const expDraft = ref('')

function onExpInput(e: Event) {
  const el = e.target as HTMLInputElement
  el.value = maskRuDate(el.value)
  expDraft.value = el.value
}

async function saveExpiry() {
  const d = detail.value
  if (!d) return
  const raw = expDraft.value.trim()
  let next: string | null = null
  if (raw) {
    next = parseRuDate(raw)
    if (!next) {
      toast.add({ title: 'Срок действия — в формате ДД.ММ.ГГГГ', color: 'error' })
      expDraft.value = formatRuDate(d.expiresAt)
      return
    }
  }
  const same = (next ? new Date(next).getTime() : null) === (d.expiresAt ? new Date(d.expiresAt).getTime() : null)
  if (same) return
  try {
    await update(d.id, { expiresAt: next })
    d.expiresAt = next
    toast.add({ title: 'Срок действия сохранён', color: 'success' })
    await changed()
  }
  catch (e) {
    toast.add({ title: errTitle(e, 'Ошибка сохранения'), color: 'error' })
  }
}

// --- Лимит девайсов --------------------------------------------------------
const limDraft = ref('')
const minLimit = computed(() => Math.max(1, detail.value?.devices.length ?? 0))

function syncForm() {
  const d = detail.value
  if (!d) return
  expDraft.value = formatRuDate(d.expiresAt)
  limDraft.value = d.deviceLimit === null ? '' : String(d.deviceLimit)
  nextTick(syncName)
}

async function saveLimit(next: number | null, withToast: boolean) {
  const d = detail.value
  if (!d) return
  if (next === d.deviceLimit) {
    limDraft.value = next === null ? '' : String(next)
    return
  }
  limDraft.value = next === null ? '' : String(next)
  try {
    await update(d.id, { deviceLimit: next })
    d.deviceLimit = next
    if (withToast) toast.add({ title: 'Лимит девайсов сохранён', color: 'success' })
    emit('changed')
  }
  catch (e) {
    toast.add({ title: errTitle(e, 'Ошибка сохранения лимита'), color: 'error' })
    limDraft.value = d.deviceLimit === null ? '' : String(d.deviceLimit)
  }
}

function onLimitChange() {
  const v = limDraft.value.trim()
  if (!v) return saveLimit(null, true)
  const n = Number.parseInt(v, 10)
  if (!(n >= minLimit.value)) {
    toast.add({ title: `Лимит — не меньше ${minLimit.value}`, color: 'error' })
    limDraft.value = detail.value?.deviceLimit == null ? '' : String(detail.value.deviceLimit)
    return
  }
  return saveLimit(n, true)
}

function limitMinus() {
  const lim = detail.value?.deviceLimit ?? null
  if (lim === null) return saveLimit(minLimit.value, false)
  if (lim > minLimit.value) return saveLimit(lim - 1, false)
}

function limitPlus() {
  const lim = detail.value?.deviceLimit ?? null
  return saveLimit(lim === null ? minLimit.value : lim + 1, false)
}

function limitInfinity() {
  const d = detail.value
  if (!d) return
  return saveLimit(d.deviceLimit === null ? Math.max(d.devices.length, 3) : null, false)
}

// --- Предпочитаемый экзит --------------------------------------------------
async function saveExit(e: Event) {
  const d = detail.value
  if (!d) return
  const el = e.target as HTMLSelectElement
  const tag = el.value || null
  try {
    await update(d.id, { preferredExit: tag })
    d.preferredExit = tag
    toast.add({ title: tag ? `Предпочитаемый экзит: ${tag.toUpperCase()}` : 'Предпочитаемый экзит: авто', color: 'success' })
    emit('changed')
  }
  catch (err) {
    toast.add({ title: errTitle(err, 'Ошибка'), color: 'error' })
    el.value = d.preferredExit ?? ''
  }
}

// --- Бот -------------------------------------------------------------------
const sendingPwd = ref(false)
async function sendPassword() {
  const d = detail.value
  if (!d) return
  sendingPwd.value = true
  try {
    await sendPasswordToTg(d.id)
    toast.add({ title: 'Приглашение отправлено в Telegram', color: 'success' })
  }
  catch (e) {
    toast.add({ title: errTitle(e, 'Ошибка отправки'), color: 'error' })
  }
  finally {
    sendingPwd.value = false
  }
}

// --- Подтверждения ---------------------------------------------------------
interface Ask { title: string, pre: string, name: string, post: string, act: string, danger?: boolean, run: () => Promise<void> }
const ask = ref<Ask | null>(null)
const askOpen = ref(false)
const askBusy = ref(false)

function confirm(a: Ask) {
  ask.value = a
  askOpen.value = true
}

async function runAsk() {
  const a = ask.value
  if (!a || askBusy.value) return
  askBusy.value = true
  try {
    await a.run()
    askOpen.value = false
  }
  catch {
    // Ошибку уже показал тост, окно остаётся открытым.
  }
  finally {
    askBusy.value = false
  }
}

async function guard(fn: () => Promise<void>, fallback: string) {
  try {
    await fn()
  }
  catch (e) {
    toast.add({ title: errTitle(e, fallback), color: 'error' })
    throw e
  }
}

function askUnlink() {
  const d = detail.value!
  confirm({
    title: 'Отозвать доступ к боту?',
    pre: 'Отвязать Telegram клиента ',
    name: d.name,
    post: '? Он потеряет доступ к самообслуживанию в боте, а старая ссылка-приглашение перестанет работать. Конфиги VPN продолжат работать.',
    act: 'Отозвать',
    run: () => guard(async () => {
      await unlink(d.id)
      d.tgLinked = false
      d.tgUsername = null
      toast.add({ title: 'Доступ к боту отозван', color: 'success' })
      emit('changed')
    }, 'Ошибка'),
  })
}

function askReissueDevice(dev: Device) {
  const d = detail.value!
  confirm({
    title: 'Перевыпустить ключи девайса?',
    pre: 'Перевыпустить ключи девайса ',
    name: dev.name,
    post: '? Старые конфиги этого девайса сразу перестанут работать.',
    act: 'Перевыпустить',
    run: () => guard(async () => {
      await reissueDevice(d.id, dev.id)
      toast.add({ title: `Ключи девайса «${dev.name}» перевыпущены`, color: 'success' })
      await changed()
    }, 'Ошибка перевыпуска'),
  })
}

function askRemoveDevice(dev: Device) {
  const d = detail.value!
  confirm({
    title: 'Удалить девайс?',
    pre: 'Удалить девайс ',
    name: dev.name,
    post: '? Действие необратимо, конфиги девайса сразу перестанут работать.',
    act: 'Удалить',
    danger: true,
    run: () => guard(async () => {
      await removeDevice(d.id, dev.id)
      toast.add({ title: `Девайс «${dev.name}» удалён`, color: 'success' })
      await changed()
    }, 'Ошибка удаления'),
  })
}

function askReissueAll() {
  const d = detail.value!
  confirm({
    title: 'Перевыпустить все ключи?',
    pre: 'Перевыпустить ключи всех девайсов клиента ',
    name: d.name,
    post: '? Старые конфиги WireGuard и OpenVPN сразу перестанут работать — нужно будет раздать новые.',
    act: 'Перевыпустить',
    run: () => guard(async () => {
      const res = await reissueAll(d.id)
      toast.add({ title: `Перевыпущены ключи всех девайсов (${res.devices})`, color: 'success' })
      await changed()
    }, 'Ошибка перевыпуска'),
  })
}

function askRemoveClient() {
  const d = detail.value!
  confirm({
    title: 'Удалить клиента?',
    pre: 'Удалить клиента ',
    name: d.name,
    post: ' со всеми девайсами? Действие необратимо, конфиги сразу перестанут работать.',
    act: 'Удалить',
    danger: true,
    run: () => guard(async () => {
      await remove(d.id)
      toast.add({ title: `Клиент «${d.name}» удалён`, color: 'success' })
      emit('deleted')
    }, 'Ошибка удаления'),
  })
}

// --- Заморозка -------------------------------------------------------------
const freezing = ref(false)
async function toggleFreeze() {
  const d = detail.value
  if (!d) return
  const next = !d.frozenManual
  freezing.value = true
  try {
    await update(d.id, { frozenManual: next })
    toast.add({ title: `Клиент «${d.name}» ${next ? 'заморожен' : 'разморожен'}`, color: 'success' })
    await changed()
  }
  catch (e) {
    toast.add({ title: errTitle(e, 'Ошибка'), color: 'error' })
  }
  finally {
    freezing.value = false
  }
}

// --- Девайсы и конфиги -----------------------------------------------------
const newDevOpen = ref(false)
const cfgOpen = ref(false)
const cfgDevice = ref<Device | null>(null)
const cfgKind = ref<ConfigKind>('wg')

function openConfig(dev: Device, kind: ConfigKind) {
  cfgDevice.value = dev
  cfgKind.value = kind
  cfgOpen.value = true
}

const limitReached = computed(() => {
  const d = detail.value
  return !!d && d.deviceLimit !== null && d.devices.length >= d.deviceLimit
})

// --- Показатели ------------------------------------------------------------
const tag = computed(() => {
  const d = detail.value
  if (!d) return null
  if (d.status === 'frozen') return { cls: '', text: 'Заморожен' }
  if (props.traffic?.online) return { cls: 'ok', text: 'В сети' }
  if (expiringInDays(d.expiresAt, d.status) !== null) return { cls: 'amb', text: 'Истекает' }
  return { cls: '', text: 'Не в сети' }
})

const rx = computed(() => props.traffic?.rxBytes ?? detail.value?.rxTotal ?? 0)
const tx = computed(() => props.traffic?.txBytes ?? detail.value?.txTotal ?? 0)

const created = computed(() => (detail.value ? new Date(detail.value.createdAt).toLocaleDateString('ru-RU') : ''))
</script>

<template>
  <div ref="root" class="cdet-in">
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
            @keydown.enter.prevent="nameEl?.blur()"
          />
          <small>{{ detail.tgLinked ? (detail.tgUsername ? `Привязан к Telegram · @${detail.tgUsername}` : 'Привязан к Telegram') : 'Telegram не привязан' }}</small>
        </div>
        <span v-if="tag" class="tag" :class="tag.cls">{{ tag.text }}</span>
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
            @keydown.enter.prevent="($event.target as HTMLInputElement).blur()"
          >
        </div>
        <div><span>Создан</span><b>{{ created }}</b></div>
        <div>
          <span>Суммарный трафик</span><b>{{ sizeRu(rx + tx) }}</b>
          <em>↓ {{ sizeRu(rx) }} · ↑ {{ sizeRu(tx) }}</em>
        </div>
        <div>
          <span>Фильтр рекламы</span><b>{{ detail.filterTraffic ? 'включён' : 'выключен' }}</b>
          <em>меняется только при создании</em>
        </div>
      </div>

      <div class="crw bt">
        <span>Бот · <b>{{ detail.tgLinked ? (detail.tgUsername ? `@${detail.tgUsername}` : 'привязан') : 'не привязан' }}</b></span>
        <div class="bta">
          <span :title="canSendTg ? undefined : tgReason">
            <button
              type="button"
              class="pill w"
              :disabled="!canSendTg || sendingPwd"
              @click="sendPassword"
            >Отправить доступ к боту</button>
          </span>
          <button
            v-if="detail.tgLinked"
            type="button"
            class="pill red"
            @click="askUnlink"
          >
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
            @click="limitMinus"
          >
            −
          </button>
          <input
            v-model="limDraft"
            class="in"
            placeholder="∞"
            inputmode="numeric"
            aria-label="Лимит девайсов"
            @change="onLimitChange"
            @keydown.enter.prevent="($event.target as HTMLInputElement).blur()"
          >
          <button type="button" aria-label="Больше" @click="limitPlus">
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
          @click="limitInfinity"
        >
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

      <div class="sub">
        <h3>Девайсы {{ detail.devices.length }} / {{ detail.deviceLimit ?? '∞' }}</h3>
        <span :title="limitReached ? 'Достигнут лимит девайсов' : undefined">
          <button
            type="button"
            class="pill"
            :disabled="limitReached"
            @click="newDevOpen = true"
          >+ девайс</button>
        </span>
      </div>
      <div class="devl scroll">
        <div v-for="dev in detail.devices" :key="dev.id" class="dev" data-testid="device-row">
          <!-- телефон: имя и ↻ 🗑 строкой, под ними три протокола -->
          <template v-if="mobile">
            <div class="kb dvt">
              <div>
                <b>{{ dev.name }}</b>
                <small :class="{ tr: dev.rxTotal + dev.txTotal > 0 }">{{ dev.rxTotal + dev.txTotal > 0 ? sizeRu(dev.rxTotal + dev.txTotal) : 'нет трафика' }}</small>
              </div>
              <button
                type="button"
                class="ic"
                aria-label="Перевыпустить ключи девайса"
                @click="askReissueDevice(dev)"
              >
                <svg class="i" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5" /></svg>
              </button>
              <button
                type="button"
                class="ic"
                aria-label="Удалить девайс"
                @click="askRemoveDevice(dev)"
              >
                <svg class="i" viewBox="0 0 24 24"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13" /></svg>
              </button>
            </div>
            <div class="kb dk3">
              <button type="button" @click="openConfig(dev, 'wg')">
                WG
              </button>
              <button type="button" @click="openConfig(dev, 'ovpn')">
                OVPN
              </button>
              <button type="button" @click="openConfig(dev, 'ike')">
                IKEv2
              </button>
            </div>
          </template>
          <template v-else>
            <div>
              <b>{{ dev.name }}</b>
              <small
                :class="{ tr: dev.rxTotal + dev.txTotal > 0 }"
                :title="dev.rxTotal + dev.txTotal > 0 ? `↓ ${sizeRu(dev.rxTotal)} ↑ ${sizeRu(dev.txTotal)}` : undefined"
              >{{ dev.rxTotal + dev.txTotal > 0 ? sizeRu(dev.rxTotal + dev.txTotal) : 'нет трафика' }}</small>
            </div>
            <div class="kb">
              <button type="button" @click="openConfig(dev, 'wg')">
                WG
              </button>
              <button type="button" @click="openConfig(dev, 'ovpn')">
                OVPN
              </button>
              <button type="button" @click="openConfig(dev, 'ike')">
                IKEv2
              </button>
              <button
                type="button"
                class="ic"
                title="Перевыпустить ключи девайса"
                aria-label="Перевыпустить ключи девайса"
                @click="askReissueDevice(dev)"
              >
                <svg class="i" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5" /></svg>
              </button>
              <button
                type="button"
                class="ic"
                title="Удалить девайс"
                aria-label="Удалить девайс"
                @click="askRemoveDevice(dev)"
              >
                <svg class="i" viewBox="0 0 24 24"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13" /></svg>
              </button>
            </div>
          </template>
        </div>
        <div v-if="!detail.devices.length" class="dev nod">
          Пока нет девайсов — добавь первый.
        </div>
      </div>

      <div class="acts">
        <button type="button" class="pill" @click="askReissueAll">
          Перевыпустить все ключи
        </button>
        <button
          type="button"
          class="pill"
          :disabled="freezing"
          @click="toggleFreeze"
        >
          {{ detail.frozenManual ? 'Разморозить' : 'Заморозить' }}
        </button>
        <button type="button" class="pill red" @click="askRemoveClient">
          Удалить клиента
        </button>
      </div>

      <ClientsNewDevice v-model:open="newDevOpen" :client-id="detail.id" @added="changed" />
      <ClientsDeviceConfig
        v-if="cfgDevice"
        v-model:open="cfgOpen"
        :client-id="detail.id"
        :device="cfgDevice"
        :kind="cfgKind"
      />
    </template>

    <ClientsModal v-model:open="askOpen" :title="ask?.title ?? ''">
      <p class="mt">
        {{ ask?.pre }}<b>«{{ ask?.name }}»</b>{{ ask?.post }}
      </p>
      <div class="mf">
        <button type="button" class="pill" @click="askOpen = false">
          Отмена
        </button>
        <button
          type="button"
          class="pill"
          :class="ask?.danger ? 'redw' : 'w'"
          :disabled="askBusy"
          @click="runAsk"
        >
          {{ ask?.act }}
        </button>
      </div>
    </ClientsModal>
  </div>
</template>
