<script setup lang="ts">
import type { ClientDetail } from '~/composables/useClients'
import { fmtBytes, useClientDetail, useClients } from '~/composables/useClients'
import { useTelegramStatus } from '~/composables/useTelegramStatus'
import { copyText } from '~/utils/clipboard'

const props = defineProps<{ clientId: number | null }>()
const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ changed: [] }>()

const { fetchDetail } = useClientDetail()
const { update, remove, reissueAll, sendPasswordToTg } = useClients()
const { canSend: canSendTg, reason: tgReason } = useTelegramStatus()
const toast = useToast()

const DEFAULT_DEVICE_LIMIT = 3

const detail = ref<ClientDetail | null>(null)
const loading = ref(false)

// Локальные редактируемые поля шапки.
const name = ref('')
const expiresAt = ref('')
const deviceLimit = ref<number>(DEFAULT_DEVICE_LIMIT)
const unlimited = ref(false)

async function load() {
  if (props.clientId == null) return
  loading.value = true
  try {
    const d = await fetchDetail(props.clientId)
    detail.value = d
    syncForm(d)
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Не удалось загрузить клиента', color: 'error' })
    open.value = false
  }
  finally {
    loading.value = false
  }
}

function syncForm(d: ClientDetail) {
  name.value = d.name
  expiresAt.value = d.expiresAt ? d.expiresAt.slice(0, 10) : ''
  unlimited.value = d.deviceLimit === null
  deviceLimit.value = d.deviceLimit ?? DEFAULT_DEVICE_LIMIT
}

watch(open, (now) => {
  if (now) {
    detail.value = null
    load()
  }
})

// --- Header edits ---------------------------------------------------------
const savingName = ref(false)
async function saveName() {
  if (!detail.value || !name.value.trim()) return
  if (name.value.trim() === detail.value.name) return
  savingName.value = true
  try {
    await update(detail.value.id, { name: name.value.trim() })
    detail.value.name = name.value.trim()
    toast.add({ title: 'Имя сохранено', color: 'success' })
    emit('changed')
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка сохранения', color: 'error' })
    name.value = detail.value.name
  }
  finally {
    savingName.value = false
  }
}

const savingExpiry = ref(false)
async function saveExpiry() {
  if (!detail.value) return
  const next = expiresAt.value ? new Date(expiresAt.value).toISOString() : null
  const cur = detail.value.expiresAt
  if (next === cur) return
  savingExpiry.value = true
  try {
    await update(detail.value.id, { expiresAt: next })
    detail.value.expiresAt = next
    toast.add({ title: 'Срок действия сохранён', color: 'success' })
    emit('changed')
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка сохранения', color: 'error' })
    expiresAt.value = cur ? cur.slice(0, 10) : ''
  }
  finally {
    savingExpiry.value = false
  }
}

// --- Device limit ---------------------------------------------------------
const savingLimit = ref(false)

// Значение в поле ввода: при безлимите поле disabled и пустое, чтобы
// показывался плейсхолдер «∞»; иначе — число.
const limitInput = computed<number | undefined>({
  get: () => (unlimited.value ? undefined : deviceLimit.value),
  set: (v) => {
    if (typeof v === 'number' && !Number.isNaN(v)) deviceLimit.value = v
  },
})

function toggleUnlimited() {
  unlimited.value = !unlimited.value
  if (!unlimited.value) deviceLimit.value = DEFAULT_DEVICE_LIMIT
  saveLimit()
}

async function saveLimit() {
  if (!detail.value) return
  const next = unlimited.value ? null : Math.max(1, Math.floor(deviceLimit.value || DEFAULT_DEVICE_LIMIT))
  if (!unlimited.value) deviceLimit.value = next as number
  if (next === detail.value.deviceLimit) return
  savingLimit.value = true
  try {
    await update(detail.value.id, { deviceLimit: next })
    detail.value.deviceLimit = next
    emit('changed')
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка сохранения лимита', color: 'error' })
    syncForm(detail.value)
  }
  finally {
    savingLimit.value = false
  }
}

// --- Password -------------------------------------------------------------
async function copyPassword() {
  if (!detail.value) return
  if (await copyText(detail.value.password))
    toast.add({ title: 'Пароль скопирован', color: 'success' })
  else
    toast.add({ title: 'Не удалось скопировать', color: 'error' })
}

const sendingPwd = ref(false)
async function sendPassword() {
  if (!detail.value) return
  sendingPwd.value = true
  try {
    await sendPasswordToTg(detail.value.id)
    toast.add({ title: 'Пароль отправлен в Telegram', color: 'success' })
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка отправки', color: 'error' })
  }
  finally {
    sendingPwd.value = false
  }
}

// --- Client actions -------------------------------------------------------
const reissuing = ref(false)
const confirmReissue = ref(false)
async function doReissueAll() {
  if (!detail.value) return
  reissuing.value = true
  try {
    const res = await reissueAll(detail.value.id)
    toast.add({ title: `Перевыпущены ключи всех девайсов (${res.devices})`, color: 'success' })
    confirmReissue.value = false
    await load()
    emit('changed')
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка перевыпуска', color: 'error' })
  }
  finally {
    reissuing.value = false
  }
}

const freezing = ref(false)
async function toggleFreeze() {
  if (!detail.value) return
  const next = !detail.value.frozenManual
  freezing.value = true
  try {
    await update(detail.value.id, { frozenManual: next })
    detail.value.frozenManual = next
    detail.value.status = next ? 'frozen' : 'active'
    toast.add({ title: next ? 'Клиент заморожен' : 'Клиент разморожен', color: 'success' })
    emit('changed')
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка', color: 'error' })
  }
  finally {
    freezing.value = false
  }
}

const confirmDelete = ref(false)
const deleting = ref(false)
async function doDelete() {
  if (!detail.value) return
  deleting.value = true
  try {
    await remove(detail.value.id)
    toast.add({ title: `Клиент «${detail.value.name}» удалён`, color: 'success' })
    confirmDelete.value = false
    open.value = false
    emit('changed')
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка удаления', color: 'error' })
  }
  finally {
    deleting.value = false
  }
}

// --- Devices --------------------------------------------------------------
const showAddDevice = ref(false)

async function onDevicesChanged() {
  await load()
  emit('changed')
}

const trafficTotal = computed(() =>
  detail.value ? detail.value.rxTotal + detail.value.txTotal : 0,
)

const limitReached = computed(() => {
  const d = detail.value
  if (!d || d.deviceLimit === null) return false
  return d.devices.length >= d.deviceLimit
})
</script>

<template>
  <UModal
    v-model:open="open"
    :title="detail?.name ?? 'Клиент'"
    :ui="{ content: 'max-w-lg' }"
  >
    <template #body>
      <div
        v-if="loading && !detail"
        class="text-(--ui-text-muted) text-sm text-center py-8"
      >
        загружаю…
      </div>

      <div v-else-if="detail" class="space-y-5">
        <!-- Header: name, dates, traffic -->
        <div class="space-y-3">
          <UFormField label="Имя клиента">
            <UInput
              v-model="name"
              class="w-full"
              :loading="savingName"
              @blur="saveName"
              @keydown.enter="saveName"
            />
          </UFormField>

          <div class="grid grid-cols-2 gap-3">
            <UFormField label="Истекает (пусто = бессрочно)">
              <UInput
                v-model="expiresAt"
                type="date"
                class="w-full"
                :loading="savingExpiry"
                @change="saveExpiry"
              />
            </UFormField>
            <div>
              <div class="text-xs text-(--ui-text-muted) mb-1">
                Создан
              </div>
              <div class="text-sm py-1.5">
                {{ new Date(detail.createdAt).toLocaleDateString('ru-RU') }}
              </div>
            </div>
          </div>

          <div class="flex items-center gap-2 text-sm">
            <UIcon name="i-lucide-arrow-down-up" class="size-4 text-(--ui-text-muted)" />
            <span class="text-(--ui-text-muted)">Суммарный трафик:</span>
            <span class="font-medium text-(--ui-text-highlighted)">
              {{ fmtBytes(trafficTotal) }}
            </span>
            <span class="text-xs text-(--ui-text-muted)">
              (↓ {{ fmtBytes(detail.rxTotal) }} ↑ {{ fmtBytes(detail.txTotal) }})
            </span>
          </div>
        </div>

        <USeparator />

        <!-- Password -->
        <UFormField label="Пароль клиента">
          <div class="flex gap-2">
            <UInput
              :model-value="detail.password"
              readonly
              class="flex-1 font-mono"
            />
            <UTooltip text="Копировать пароль">
              <UButton
                icon="i-lucide-copy"
                color="neutral"
                variant="soft"
                @click="copyPassword"
              />
            </UTooltip>
            <UTooltip :text="canSendTg ? 'Отправить пароль в Telegram' : tgReason" :disabled="false">
              <UButton
                icon="i-lucide-send"
                color="neutral"
                variant="soft"
                :disabled="!canSendTg"
                :loading="sendingPwd"
                @click="sendPassword"
              >
                В TG
              </UButton>
            </UTooltip>
          </div>
        </UFormField>

        <!-- Device limit -->
        <UFormField label="Лимит девайсов">
          <div class="flex gap-2 items-center">
            <UButtonGroup>
              <UButton
                icon="i-lucide-minus"
                color="neutral"
                variant="soft"
                :disabled="unlimited || deviceLimit <= 1"
                @click="deviceLimit = Math.max(1, deviceLimit - 1); saveLimit()"
              />
              <UInput
                v-model.number="limitInput"
                type="number"
                :min="1"
                class="w-20"
                :disabled="unlimited"
                :placeholder="unlimited ? '∞' : ''"
                :ui="{ base: 'text-center' }"
                @change="saveLimit"
              />
              <UButton
                icon="i-lucide-plus"
                color="neutral"
                variant="soft"
                :disabled="unlimited"
                @click="deviceLimit = deviceLimit + 1; saveLimit()"
              />
            </UButtonGroup>
            <UButton
              :color="unlimited ? 'primary' : 'neutral'"
              :variant="unlimited ? 'solid' : 'soft'"
              :loading="savingLimit"
              @click="toggleUnlimited"
            >
              Безлимитно
            </UButton>
            <span v-if="unlimited" class="text-(--ui-text-muted) text-sm">
              лимит снят (∞)
            </span>
          </div>
        </UFormField>

        <USeparator />

        <!-- Client actions -->
        <div class="flex flex-wrap gap-2">
          <UButton
            icon="i-lucide-rotate-cw"
            color="neutral"
            variant="soft"
            @click="confirmReissue = true"
          >
            Перевыпустить все ключи
          </UButton>
          <UButton
            :icon="detail.frozenManual ? 'i-lucide-play' : 'i-lucide-snowflake'"
            color="neutral"
            variant="soft"
            :loading="freezing"
            @click="toggleFreeze"
          >
            {{ detail.frozenManual ? 'Разморозить' : 'Заморозить' }}
          </UButton>
          <UButton
            icon="i-lucide-trash-2"
            color="error"
            variant="soft"
            @click="confirmDelete = true"
          >
            Удалить клиента
          </UButton>
        </div>

        <USeparator />

        <!-- Devices -->
        <div class="space-y-2">
          <div class="flex items-center justify-between">
            <div class="font-semibold text-sm">
              Девайсы
              <span class="text-(--ui-text-muted) font-normal">
                {{ detail.devices.length }}<template v-if="detail.deviceLimit !== null"> / {{ detail.deviceLimit }}</template>
              </span>
            </div>
            <UTooltip
              :text="limitReached ? 'Достигнут лимит девайсов' : 'Добавить девайс'"
              :disabled="!limitReached"
            >
              <UButton
                icon="i-lucide-plus"
                size="xs"
                color="primary"
                variant="soft"
                :disabled="limitReached"
                @click="showAddDevice = true"
              >
                девайс
              </UButton>
            </UTooltip>
          </div>

          <div
            v-if="detail.devices.length === 0"
            class="text-(--ui-text-muted) text-sm py-4 text-center"
          >
            Пока нет девайсов — добавь первый.
          </div>
          <div v-else class="space-y-2 max-h-[280px] overflow-y-auto pr-1">
            <ClientsDeviceCard
              v-for="device in detail.devices"
              :key="device.id"
              :client-id="detail.id"
              :device="device"
              @changed="onDevicesChanged"
            />
          </div>
        </div>
      </div>
    </template>
  </UModal>

  <!-- Add device dialog -->
  <ClientsAddDeviceDialog
    v-if="detail"
    v-model:open="showAddDevice"
    :client-id="detail.id"
    @added="onDevicesChanged"
  />

  <!-- Reissue-all confirm -->
  <UModal v-model:open="confirmReissue" title="Перевыпустить все ключи?">
    <template #body>
      <p class="text-sm">
        Перевыпустить ключи всех девайсов клиента
        <span class="font-semibold">«{{ detail?.name }}»</span>?
        Старые конфиги WireGuard и OpenVPN сразу перестанут работать —
        нужно будет раздать новые.
      </p>
    </template>
    <template #footer>
      <UButton color="neutral" variant="soft" @click="confirmReissue = false">
        Отмена
      </UButton>
      <UButton color="primary" :loading="reissuing" @click="doReissueAll">
        Перевыпустить
      </UButton>
    </template>
  </UModal>

  <!-- Delete confirm -->
  <UModal v-model:open="confirmDelete" title="Удалить клиента?">
    <template #body>
      <p class="text-sm">
        Удалить клиента
        <span class="font-semibold">«{{ detail?.name }}»</span>
        со всеми девайсами? Действие необратимо, конфиги сразу перестанут работать.
      </p>
    </template>
    <template #footer>
      <UButton color="neutral" variant="soft" @click="confirmDelete = false">
        Отмена
      </UButton>
      <UButton color="error" :loading="deleting" @click="doDelete">
        Удалить
      </UButton>
    </template>
  </UModal>
</template>
