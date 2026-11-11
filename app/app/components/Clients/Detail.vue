<script setup lang="ts">
import type { ClientDetail, ClientTraffic, Device } from '~/composables/useClients'
import { useClientDetail, useClients } from '~/composables/useClients'
import { anim, errTitle, expiringInDays, initials } from '~/utils/clients-view'
import { dateRuLocal, formatRuDateUtc, parseRuDateUtc, sizeBinRu } from '~/utils/format'
import { maskRuDate } from '~/utils/ru-date-mask'
import type { ConfigKind } from '~/composables/useDeviceAccess'
import { accessStatus } from '#shared/access-operation'
import type { AccessOperation } from '#shared/access-operation'
import type { Ask } from './Ask.vue'

const props = defineProps<{ clientId: number, traffic: ClientTraffic | null, exits: string[] }>()
const emit = defineEmits<{ changed: [], deleting: [id: number, accepted: Promise<{ operation: AccessOperation }>], deleted: [id: number], gone: [id: number] }>()

const { fetchDetail } = useClientDetail()
const { update } = useClients()
const toast = useToast()

const root = ref<HTMLElement>()
const nameEl = ref<HTMLElement>()
const detail = ref<ClientDetail | null>(null)
let loadVersion = 0
// Выбран другой клиент, а на экране ещё прежний: его поля не правятся,
// иначе введённое уходило бы прежнему клиенту.
const stale = computed(() => detail.value !== null && detail.value.id !== props.clientId)

/** Клиента уже нет: удалён в другой вкладке или другим админом. */
function isGone(e: unknown) {
  return (e as { statusCode?: number }).statusCode === 404
}

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
    const d = await fetchDetail(props.clientId)
    if (version !== loadVersion) return
    const prev = detail.value
    detail.value = d
    syncForm(prev)
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
let operationTimer: ReturnType<typeof setInterval> | undefined
onMounted(() => { operationTimer = setInterval(() => { void load() }, 3000) })
onUnmounted(() => { clearInterval(operationTimer) })

async function changed() {
  await load()
  emit('changed')
}

// --- Имя: правится прямо в заголовке ---------------------------------------
// Опрос карточки не затирает несохранённое имя (например, после 409): как и у
// срока с лимитом, сервер берётся, только если в заголовке показанное до опроса.
function syncName(same: ClientDetail | null) {
  const el = nameEl.value
  const d = detail.value
  if (!el || !d || document.activeElement === el) return
  if (!same || !el.textContent || el.textContent === same.name) el.textContent = d.name
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
    const res = await update(d.id, { name: v })
    d.name = v
    el.textContent = v
    toast.add(doneToast('Имя сохранено', res.warning))
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
    next = parseRuDateUtc(raw)
    if (!next) {
      toast.add({ title: 'Срок действия — в формате ДД.ММ.ГГГГ', color: 'error' })
      expDraft.value = formatRuDateUtc(d.expiresAt)
      return
    }
  }
  const same = (next ? new Date(next).getTime() : null) === (d.expiresAt ? new Date(d.expiresAt).getTime() : null)
  if (same) return
  try {
    const res = await update(d.id, { expiresAt: next })
    d.expiresAt = next
    toast.add(doneToast('Срок действия сохранён', res.warning))
    await changed()
  }
  catch (e) {
    toast.add({ title: errTitle(e, 'Ошибка сохранения'), color: 'error' })
  }
}

const expText = (d: ClientDetail) => formatRuDateUtc(d.expiresAt)

// Карточка перезагружается после сохранения срока или девайса, когда человек
// уже мог начать править соседнее поле: черновик берёт значение сервера, только
// если в нём то, что было показано до перезагрузки. Другой клиент — поля заново.
// Лимит так же ведёт себя сам (DetailLimit.vue).
function syncForm(prev: ClientDetail | null) {
  const d = detail.value
  if (!d) return
  const same = prev?.id === d.id ? prev : null
  if (!same || expDraft.value === expText(same)) expDraft.value = expText(d)
  nextTick(() => syncName(same))
}

function onLimitSaved(id: number, limit: number | null) {
  if (detail.value?.id === id) detail.value.deviceLimit = limit
  emit('changed')
}

// --- Предпочитаемый экзит --------------------------------------------------
async function saveExit(e: Event) {
  const d = detail.value
  if (!d) return
  const el = e.target as HTMLSelectElement
  const tag = el.value || null
  try {
    const res = await update(d.id, { preferredExit: tag })
    d.preferredExit = tag
    toast.add(doneToast(tag ? `Предпочитаемый экзит: ${tag.toUpperCase()}` : 'Предпочитаемый экзит: авто', res.warning))
    emit('changed')
  }
  catch (err) {
    toast.add({ title: errTitle(err, 'Ошибка'), color: 'error' })
    el.value = d.preferredExit ?? ''
  }
}

// --- Части карточки --------------------------------------------------------
const asker = ref<{ confirm: (a: Ask) => void }>()
const confirm = (a: Ask) => asker.value?.confirm(a)

function onUnlinked(id: number) {
  const d = detail.value
  if (d?.id === id) {
    d.tgLinked = false
    d.tgUsername = null
  }
  emit('changed')
}

// Пока диалог удаления был открыт, опрос мог подменить карточку новым объектом.
function onDeleteAccepted(id: number, operation: AccessOperation) {
  const d = detail.value
  if (d?.id !== id) return
  d.operation = operation
  d.deleting = true
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

// --- Показатели ------------------------------------------------------------
const tag = computed(() => {
  const d = detail.value
  if (!d) return null
  if (d.status === 'frozen') return { cls: '', text: d.operation ? accessStatus(d.operation) : 'Состояние не подтверждено' }
  if (props.traffic?.online) return { cls: 'ok', text: 'В сети' }
  if (expiringInDays(d.expiresAt, d.status) !== null) return { cls: 'amb', text: 'Истекает' }
  return { cls: '', text: 'Не в сети' }
})

const rx = computed(() => props.traffic?.rxBytes ?? detail.value?.rxTotal ?? 0)
const tx = computed(() => props.traffic?.txBytes ?? detail.value?.txTotal ?? 0)

const created = computed(() => (detail.value ? dateRuLocal(detail.value.createdAt) : ''))
</script>

<template>
  <div ref="root" class="cdet-in" :inert="stale" :aria-busy="stale">
    <template v-if="detail">
      <div class="cdh">
        <span class="av">{{ initials(detail.name) }}</span>
        <div style="min-width:0">
          <h2
            ref="nameEl" contenteditable="plaintext-only" spellcheck="false" title="Имя клиента" @blur="saveName"
            @keydown.enter.prevent="nameEl?.blur()" />
          <small>{{ detail.tgLinked ? (detail.tgUsername ? `Привязан к Telegram · @${detail.tgUsername}` : 'Привязан к Telegram') : 'Telegram не привязан' }}</small>
        </div>
        <span v-if="tag?.text" class="tag" :class="`access-tag ${tag.cls}`">{{ tag.text }}</span>
      </div>

      <div class="kgrid">
        <div>
          <span>Истекает</span>
          <input
            :value="expDraft" class="in" placeholder="бессрочно" title="Пусто = бессрочно" aria-label="Истекает" inputmode="numeric" maxlength="10"
            @input="onExpInput" @blur="saveExpiry" @keydown.enter.prevent="($event.target as HTMLInputElement).blur()">
        </div>
        <div><span>Создан</span><b>{{ created }}</b></div>
        <div>
          <span>Суммарный трафик</span><b>{{ sizeBinRu(rx + tx) }}</b>
          <em>↓ {{ sizeBinRu(rx) }} · ↑ {{ sizeBinRu(tx) }}</em>
        </div>
        <div>
          <span>Фильтр рекламы</span><b>{{ detail.filterTraffic ? 'включён' : 'выключен' }}</b>
          <em>меняется только при создании</em>
        </div>
      </div>

      <ClientsOperationStatus :operation="detail.operation" :deleting="detail.deleting" @changed="changed" />

      <ClientsDetailBot :detail="detail" :confirm="confirm" @unlinked="onUnlinked" />

      <ClientsDetailLimit :detail="detail" @saved="onLimitSaved" />

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

      <ClientsDetailDevices :detail="detail" :confirm="confirm" :reload="changed" @add="newDevOpen = true" @config="openConfig" />

      <ClientsDetailActions
        :detail="detail" :confirm="confirm" :reload="changed" @deleting="(id, accepted) => emit('deleting', id, accepted)"
        @accepted="onDeleteAccepted" />

      <ClientsNewDevice v-model:open="newDevOpen" :client-id="detail.id" @added="changed" />
      <ClientsDeviceConfig v-if="cfgDevice" v-model:open="cfgOpen" :client-id="detail.id" :device="cfgDevice" :kind="cfgKind" />
    </template>

    <ClientsAsk ref="asker" />
  </div>
</template>
