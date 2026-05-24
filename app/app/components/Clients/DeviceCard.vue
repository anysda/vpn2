<script setup lang="ts">
import type { Device } from '~/composables/useClients'
import { fmtBytes, useClientDevices } from '~/composables/useClients'
import { useClientsIkev2 } from '~/composables/useClientsIkev2'
import { useClientsOvpn } from '~/composables/useClientsOvpn'
import { useClientsWg } from '~/composables/useClientsWg'
import { useTelegramStatus } from '~/composables/useTelegramStatus'
import { copyText } from '~/utils/clipboard'

const props = defineProps<{
  clientId: number
  device: Device
}>()
const emit = defineEmits<{ changed: [] }>()

const { removeDevice, reissueDevice } = useClientDevices()
const { getWgConfig, sendWgToTg, wgQrUrl } = useClientsWg()
const { getOvpnConfig, sendOvpnToTg } = useClientsOvpn()
const { getIkev2Credentials, ikev2CaCrtUrl, sendIkev2ToTg } = useClientsIkev2()
const { canSend: canSendTg, reason: tgReason } = useTelegramStatus()
const toast = useToast()

const trafficTotal = computed(() => props.device.rxTotal + props.device.txTotal)

// --- WireGuard modal ------------------------------------------------------
const showWg = ref(false)
const wgConf = ref('')
const wgLoading = ref(false)
const wgSending = ref(false)

async function loadWg() {
  if (wgConf.value) return
  wgLoading.value = true
  try {
    wgConf.value = await getWgConfig(props.clientId, props.device.id)
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Не удалось получить .conf', color: 'error' })
  }
  finally {
    wgLoading.value = false
  }
}

watch(showWg, (now) => {
  if (now) loadWg()
})

async function copyWg() {
  if (!wgConf.value) return
  if (await copyText(wgConf.value))
    toast.add({ title: '.conf скопирован', color: 'success' })
  else
    toast.add({ title: 'Не удалось скопировать', color: 'error' })
}

function downloadWg() {
  if (!wgConf.value) return
  const blob = new Blob([wgConf.value], { type: 'text/plain' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${props.device.configName}.conf`
  a.click()
  URL.revokeObjectURL(a.href)
}

async function sendWg() {
  wgSending.value = true
  try {
    await sendWgToTg(props.clientId, props.device.id)
    toast.add({ title: 'WireGuard-конфиг отправлен в Telegram', color: 'success' })
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка отправки', color: 'error' })
  }
  finally {
    wgSending.value = false
  }
}

// --- OpenVPN modal --------------------------------------------------------
const showOvpn = ref(false)
const ovpnConf = ref('')
const ovpnLoading = ref(false)
const ovpnSending = ref(false)

async function loadOvpn() {
  if (ovpnConf.value) return
  ovpnLoading.value = true
  try {
    ovpnConf.value = await getOvpnConfig(props.clientId, props.device.id)
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Не удалось получить .ovpn', color: 'error' })
  }
  finally {
    ovpnLoading.value = false
  }
}

watch(showOvpn, (now) => {
  if (now) loadOvpn()
})

async function copyOvpn() {
  if (!ovpnConf.value) return
  if (await copyText(ovpnConf.value))
    toast.add({ title: '.ovpn скопирован', color: 'success' })
  else
    toast.add({ title: 'Не удалось скопировать', color: 'error' })
}

function downloadOvpn() {
  if (!ovpnConf.value) return
  const blob = new Blob([ovpnConf.value], { type: 'application/x-openvpn-profile' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${props.device.configName}.ovpn`
  a.click()
  URL.revokeObjectURL(a.href)
}

async function sendOvpn() {
  ovpnSending.value = true
  try {
    await sendOvpnToTg(props.clientId, props.device.id)
    toast.add({ title: 'OpenVPN-конфиг отправлен в Telegram', color: 'success' })
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка отправки', color: 'error' })
  }
  finally {
    ovpnSending.value = false
  }
}

// --- IKEv2 modal ----------------------------------------------------------
const showIkev2 = ref(false)
const ikev2Creds = ref<{ server: string, username: string, password: string, hasCa: boolean } | null>(null)
const ikev2Loading = ref(false)
const ikev2Sending = ref(false)
const showIkev2Pass = ref(false)

async function loadIkev2() {
  if (ikev2Creds.value) return
  ikev2Loading.value = true
  try {
    const r = await getIkev2Credentials(props.clientId, props.device.id)
    // hasCa=true → self-signed режим, нужна кнопка «Скачать CA».
    // hasCa=false → letsencrypt, корень в trust-store клиента, кнопка скрыта.
    ikev2Creds.value = { server: r.server, username: r.username, password: r.password, hasCa: r.caCertPem !== null }
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Не удалось получить IKEv2-креды', color: 'error' })
  }
  finally {
    ikev2Loading.value = false
  }
}

watch(showIkev2, (now) => {
  if (now) loadIkev2()
})

async function copyIkev2Field(label: string, value: string | undefined) {
  if (!value) return
  if (await copyText(value)) toast.add({ title: `${label} скопирован`, color: 'success' })
  else toast.add({ title: 'Не удалось скопировать', color: 'error' })
}

async function sendIkev2() {
  ikev2Sending.value = true
  try {
    await sendIkev2ToTg(props.clientId, props.device.id)
    toast.add({ title: 'IKEv2 отправлен в Telegram', color: 'success' })
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка отправки', color: 'error' })
  }
  finally {
    ikev2Sending.value = false
  }
}

// --- Reissue / delete -----------------------------------------------------
const reissuing = ref(false)
const confirmReissue = ref(false)
async function doReissue() {
  reissuing.value = true
  try {
    await reissueDevice(props.clientId, props.device.id)
    // Сброс кешей конфигов — ключи изменились.
    wgConf.value = ''
    ovpnConf.value = ''
    ikev2Creds.value = null
    toast.add({ title: `Ключи девайса «${props.device.name}» перевыпущены`, color: 'success' })
    confirmReissue.value = false
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

const confirmDelete = ref(false)
const deleting = ref(false)
async function doDelete() {
  deleting.value = true
  try {
    await removeDevice(props.clientId, props.device.id)
    toast.add({ title: `Девайс «${props.device.name}» удалён`, color: 'success' })
    confirmDelete.value = false
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
</script>

<template>
  <div class="rounded-md border border-(--ui-border) bg-(--ui-page) px-3 py-2">
    <div class="flex items-center gap-3">
      <div class="flex-1 min-w-0">
        <div class="font-medium text-(--ui-text-highlighted) leading-tight flex items-center gap-1.5">
          <UIcon name="i-lucide-smartphone" class="size-4 text-(--ui-text-muted) shrink-0" />
          <span class="truncate">{{ device.name }}</span>
        </div>
        <div class="text-xs text-(--ui-text-muted) flex gap-3 items-center mt-0.5">
          <UTooltip
            v-if="trafficTotal > 0"
            :text="`Трафик за всё время · ↓ ${fmtBytes(device.rxTotal)} ↑ ${fmtBytes(device.txTotal)}`"
          >
            <span class="flex items-center gap-0.5">
              <UIcon name="i-lucide-arrow-down" class="size-3 shrink-0" />
              {{ fmtBytes(trafficTotal) }}
            </span>
          </UTooltip>
          <span v-else>нет трафика</span>
        </div>
      </div>
    </div>

    <div class="flex gap-1 mt-1.5 justify-end">
      <UTooltip text="IKEv2 — сервер/логин/пароль, нативный VPN-клиент iOS/macOS/Windows/Android">
        <UButton size="xs" color="neutral" variant="ghost" class="!px-1" @click="showIkev2 = true">
          <UIcon name="i-lucide-shield-check" class="size-4" />
        </UButton>
      </UTooltip>
      <UTooltip text="WireGuard — QR, .conf, копировать, отправить в Telegram">
        <UButton size="xs" color="neutral" variant="ghost" class="!px-1" @click="showWg = true">
          <WireguardLogo class="size-4" />
        </UButton>
      </UTooltip>
      <UTooltip text="OpenVPN — .ovpn-файл, копировать, отправить в Telegram">
        <UButton size="xs" color="neutral" variant="ghost" class="!px-1" @click="showOvpn = true">
          <OpenVpnLogo class="size-4" />
        </UButton>
      </UTooltip>
      <UTooltip text="Перевыпустить ключи девайса">
        <UButton
          icon="i-lucide-rotate-cw"
          size="xs"
          color="neutral"
          variant="ghost"
          :loading="reissuing"
          @click="confirmReissue = true"
        />
      </UTooltip>
      <UTooltip text="Удалить девайс">
        <UButton
          icon="i-lucide-trash-2"
          size="xs"
          color="error"
          variant="ghost"
          @click="confirmDelete = true"
        />
      </UTooltip>
    </div>

    <!-- WireGuard modal -->
    <UModal v-model:open="showWg" :title="device.name" :ui="{ content: 'max-w-md' }">
      <template #body>
        <div class="space-y-4">
          <div class="flex items-center justify-center gap-2">
            <WireguardLogo class="size-6" />
            <span class="font-semibold text-(--ui-text-highlighted)">WireGuard</span>
          </div>

          <p class="text-xs text-(--ui-text-muted) text-center">
            Отсканируй QR в приложении WireGuard или скачай .conf и импортируй.
          </p>

          <div class="flex justify-center">
            <img
              :src="wgQrUrl(clientId, device.id)"
              alt="WireGuard QR"
              class="w-64 h-64 rounded-md bg-white p-2"
            >
          </div>

          <div class="grid grid-cols-3 gap-2">
            <UButton
              block
              icon="i-lucide-copy"
              :disabled="!wgConf"
              :loading="wgLoading"
              @click="copyWg"
            >
              Копировать
            </UButton>
            <UButton
              block
              icon="i-lucide-download"
              variant="soft"
              color="neutral"
              :disabled="!wgConf"
              @click="downloadWg"
            >
              Скачать
            </UButton>
            <UButton
              block
              icon="i-lucide-send"
              variant="soft"
              color="neutral"
              :disabled="!wgConf || !canSendTg"
              :loading="wgSending"
              @click="sendWg"
            >
              В TG
            </UButton>
          </div>
          <p
            v-if="!canSendTg"
            class="text-xs text-(--ui-text-muted) text-center"
          >
            {{ tgReason }}
          </p>
        </div>
      </template>
    </UModal>

    <!-- OpenVPN modal -->
    <UModal v-model:open="showOvpn" :title="device.name" :ui="{ content: 'max-w-md' }">
      <template #body>
        <div class="space-y-4">
          <div class="flex items-center justify-center gap-2">
            <OpenVpnLogo class="size-6" />
            <span class="font-semibold text-(--ui-text-highlighted)">OpenVPN</span>
          </div>

          <p class="text-xs text-(--ui-text-muted) text-center">
            .ovpn-файл с встроенными сертификатами — импортируй в OpenVPN Connect
            или <span class="font-mono">openvpn3</span>.
          </p>

          <div
            v-if="ovpnLoading"
            class="text-(--ui-text-muted) text-sm text-center py-4"
          >
            генерирую сертификат…
          </div>

          <div v-else class="grid grid-cols-3 gap-2">
            <UButton
              block
              icon="i-lucide-copy"
              :disabled="!ovpnConf"
              @click="copyOvpn"
            >
              Копировать
            </UButton>
            <UButton
              block
              icon="i-lucide-download"
              variant="soft"
              color="neutral"
              :disabled="!ovpnConf"
              @click="downloadOvpn"
            >
              Скачать
            </UButton>
            <UButton
              block
              icon="i-lucide-send"
              variant="soft"
              color="neutral"
              :disabled="!ovpnConf || !canSendTg"
              :loading="ovpnSending"
              @click="sendOvpn"
            >
              В TG
            </UButton>
          </div>
          <p
            v-if="!canSendTg"
            class="text-xs text-(--ui-text-muted) text-center"
          >
            {{ tgReason }}
          </p>
        </div>
      </template>
    </UModal>

    <!-- IKEv2 modal -->
    <UModal v-model:open="showIkev2" :title="device.name" :ui="{ content: 'max-w-md' }">
      <template #body>
        <div class="space-y-4">
          <div class="flex items-center justify-center gap-2">
            <UIcon name="i-lucide-shield-check" class="size-6" />
            <span class="font-semibold text-(--ui-text-highlighted)">IKEv2</span>
          </div>

          <p class="text-xs text-(--ui-text-muted) text-center">
            Нативный IKEv2 в iOS / macOS / Android / Windows. Введи поля в
            настройках VPN.
            <template v-if="ikev2Creds?.hasCa">
              CA-сертификат — доверить в системе (для self-signed-сервера).
            </template>
          </p>

          <div v-if="ikev2Loading" class="text-(--ui-text-muted) text-sm text-center py-4">
            генерирую креды…
          </div>

          <div v-else-if="ikev2Creds" class="space-y-3">
            <div class="grid grid-cols-[5rem_1fr_auto] items-center gap-2 text-sm">
              <span class="text-(--ui-text-muted)">Сервер:</span>
              <span class="font-mono truncate">{{ ikev2Creds.server }}</span>
              <div class="flex">
                <!-- invisible-плейсхолдер ровно той же ширины что и eye-кнопка
                     у строки пароля — чтобы copy всех трёх строк стоял на
                     одной вертикали. -->
                <UButton icon="i-lucide-eye" size="xs" variant="ghost" class="invisible" tabindex="-1" />
                <UButton
                  icon="i-lucide-copy"
                  size="xs"
                  color="neutral"
                  variant="ghost"
                  @click="copyIkev2Field('Сервер', ikev2Creds.server)"
                />
              </div>

              <span class="text-(--ui-text-muted)">Логин:</span>
              <span class="font-mono truncate">{{ ikev2Creds.username }}</span>
              <div class="flex">
                <UButton icon="i-lucide-eye" size="xs" variant="ghost" class="invisible" tabindex="-1" />
                <UButton
                  icon="i-lucide-copy"
                  size="xs"
                  color="neutral"
                  variant="ghost"
                  @click="copyIkev2Field('Логин', ikev2Creds.username)"
                />
              </div>

              <span class="text-(--ui-text-muted)">Пароль:</span>
              <span class="font-mono truncate">{{ showIkev2Pass ? ikev2Creds.password : '•'.repeat(20) }}</span>
              <div class="flex">
                <UButton
                  :icon="showIkev2Pass ? 'i-lucide-eye-off' : 'i-lucide-eye'"
                  size="xs"
                  color="neutral"
                  variant="ghost"
                  @click="showIkev2Pass = !showIkev2Pass"
                />
                <UButton
                  icon="i-lucide-copy"
                  size="xs"
                  color="neutral"
                  variant="ghost"
                  @click="copyIkev2Field('Пароль', ikev2Creds.password)"
                />
              </div>
            </div>

            <div :class="ikev2Creds.hasCa ? 'grid grid-cols-2 gap-2' : ''">
              <UButton
                v-if="ikev2Creds.hasCa"
                block
                icon="i-lucide-file-key-2"
                variant="soft"
                color="neutral"
                :to="ikev2CaCrtUrl()"
                external
              >
                Скачать CA
              </UButton>
              <UButton
                block
                icon="i-lucide-send"
                variant="soft"
                color="neutral"
                :disabled="!canSendTg"
                :loading="ikev2Sending"
                @click="sendIkev2"
              >
                В TG
              </UButton>
            </div>
            <p v-if="!canSendTg" class="text-xs text-(--ui-text-muted) text-center">
              {{ tgReason }}
            </p>
          </div>
        </div>
      </template>
    </UModal>

    <!-- Reissue confirm -->
    <UModal v-model:open="confirmReissue" title="Перевыпустить ключи девайса?">
      <template #body>
        <p class="text-sm">
          Перевыпустить ключи девайса
          <span class="font-semibold">«{{ device.name }}»</span>?
          Старые конфиги WireGuard и OpenVPN сразу перестанут работать.
        </p>
      </template>
      <template #footer>
        <UButton color="neutral" variant="soft" @click="confirmReissue = false">
          Отмена
        </UButton>
        <UButton color="primary" :loading="reissuing" @click="doReissue">
          Перевыпустить
        </UButton>
      </template>
    </UModal>

    <!-- Delete confirm -->
    <UModal v-model:open="confirmDelete" title="Удалить девайс?">
      <template #body>
        <p class="text-sm">
          Удалить девайс <span class="font-semibold">«{{ device.name }}»</span>?
          Действие необратимо, конфиги девайса сразу перестанут работать.
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
  </div>
</template>
