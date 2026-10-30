<script setup lang="ts">
import type { Device } from '~/composables/useClients'
import { useClientsIkev2 } from '~/composables/useClientsIkev2'
import { useClientsOvpn } from '~/composables/useClientsOvpn'
import { useClientsWg } from '~/composables/useClientsWg'
import { useTelegramStatus } from '~/composables/useTelegramStatus'
import { copyText } from '~/utils/clipboard'
import { anim } from '~/utils/clients-view'

export type ConfigKind = 'wg' | 'ovpn' | 'ike'

const props = defineProps<{ clientId: number, device: Device, kind: ConfigKind }>()
const open = defineModel<boolean>('open', { default: false })

const { getWgConfig, sendWgToTg, wgQrUrl } = useClientsWg()
const { getOvpnConfig, sendOvpnToTg } = useClientsOvpn()
const { getIkev2Credentials, ikev2CaCrtUrl, sendIkev2ToTg } = useClientsIkev2()
const { canSend: canSendTg, reason: tgReason } = useTelegramStatus()
const toast = useToast()

const NAMES: Record<ConfigKind, string> = { wg: 'WireGuard', ovpn: 'OpenVPN', ike: 'IKEv2' }
const title = computed(() => `${NAMES[props.kind]} · ${props.device.name}`)

const conf = ref('')
const loading = ref(false)
const sending = ref(false)
const hasAAAA = ref(false)
const qrSrc = ref('')
// Окно 460 px вмещает QR до 97 модулей по 4 px вместе с тихой зоной (89 без неё): naturalWidth до 388 px; крупнее - окно шире.
const qrWide = ref(false)
const creds = ref<{ server: string, remoteId: string, username: string, password: string, hasCa: boolean, certificatePending: boolean } | null>(null)
const showPass = ref(false)
let request = 0

function fail(e: unknown, fallback: string) {
  const err = e as { statusMessage?: string }
  toast.add({ title: err.statusMessage ?? fallback, color: 'error' })
}

async function loadWgWarning(r: number) {
  const result = await $fetch<{ hasAAAA: boolean }>('/api/wg-endpoint-warning', { timeout: 1000, retry: 0 })
    .catch(() => ({ hasAAAA: false }))
  if (r === request) hasAAAA.value = result.hasAAAA
}

async function load() {
  const r = ++request
  conf.value = ''
  creds.value = null
  hasAAAA.value = false
  showPass.value = false
  loading.value = true
  const { clientId, device, kind } = props
  try {
    if (kind === 'wg') {
      // Адрес QR постоянный: без метки браузер показал бы из кэша старый ключ после перевыпуска.
      qrSrc.value = `${wgQrUrl(clientId, device.id)}?t=${Date.now()}`
      qrWide.value = false
      void loadWgWarning(r)
      const c = await getWgConfig(clientId, device.id)
      if (r === request) conf.value = c
    }
    else if (kind === 'ovpn') {
      const c = await getOvpnConfig(clientId, device.id)
      if (r === request) conf.value = c
    }
    else {
      const c = await getIkev2Credentials(clientId, device.id)
      // caCertPem есть только у self-signed: тогда нужна кнопка «Скачать CA».
      if (r === request) creds.value = { ...c, hasCa: c.caCertPem !== null }
    }
  }
  catch (e) {
    if (r === request) fail(e, kind === 'wg' ? 'Не удалось получить .conf' : kind === 'ovpn' ? 'Не удалось получить .ovpn' : 'Не удалось получить IKEv2-креды')
  }
  finally {
    if (r === request) loading.value = false
  }
}

watch(open, (now) => {
  if (now) void load()
  else request++
}, { immediate: true })

const ext = computed(() => (props.kind === 'wg' ? 'conf' : 'ovpn'))
const fileName = computed(() => `${props.device.configName}.${ext.value}`)

async function copyConf() {
  if (!conf.value) return
  if (await copyText(conf.value)) toast.add({ title: `.${ext.value} скопирован`, color: 'success' })
  else toast.add({ title: 'Не удалось скопировать', color: 'error' })
}

function download() {
  if (!conf.value) return
  const type = props.kind === 'wg' ? 'text/plain' : 'application/x-openvpn-profile'
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([conf.value], { type }))
  a.download = fileName.value
  a.click()
  URL.revokeObjectURL(a.href)
  toast.add({ title: `Скачивается ${fileName.value}`, color: 'success' })
}

async function sendTg() {
  sending.value = true
  const { clientId, device, kind } = props
  try {
    if (kind === 'wg') await sendWgToTg(clientId, device.id)
    else if (kind === 'ovpn') await sendOvpnToTg(clientId, device.id)
    else await sendIkev2ToTg(clientId, device.id)
    toast.add({ title: kind === 'wg' ? 'WireGuard-конфиг отправлен в Telegram' : kind === 'ovpn' ? 'OpenVPN-конфиг отправлен в Telegram' : 'IKEv2 отправлен в Telegram', color: 'success' })
  }
  catch (e) {
    fail(e, 'Ошибка отправки')
  }
  finally {
    sending.value = false
  }
}

// Копирование поля: значок на секунду становится галочкой.
const copied = ref<string | null>(null)
let copiedTimer: ReturnType<typeof setTimeout> | undefined
async function copyField(label: string, value: string, btn: HTMLElement) {
  if (!(await copyText(value))) {
    toast.add({ title: 'Не удалось скопировать', color: 'error' })
    return
  }
  toast.add({ title: `${label} скопирован`, color: 'success' })
  copied.value = label
  clearTimeout(copiedTimer)
  copiedTimer = setTimeout(() => { copied.value = null }, 1200)
  nextTick(() => anim(btn.firstElementChild, [{ opacity: 0, transform: 'scale(.6)' }, { opacity: 1, transform: 'none' }], { duration: 220 }))
}
onUnmounted(() => clearTimeout(copiedTimer))

async function copyAll() {
  const c = creds.value
  if (!c) return
  const text = `Сервер: ${c.server}\nRemote ID: ${c.remoteId}\nЛогин: ${c.username}\nПароль: ${c.password}`
  if (await copyText(text)) toast.add({ title: 'IKEv2 креды скопированы', color: 'success' })
  else toast.add({ title: 'Не удалось скопировать', color: 'error' })
}

const ikeRows = computed(() => {
  const c = creds.value
  if (!c) return []
  return [
    { k: 'Remote ID (iOS) / Server identity (Android)', v: c.remoteId, lab: 'Remote ID', sec: false },
    { k: 'Сервер', v: c.server, lab: 'Сервер', sec: false },
    { k: 'Логин', v: c.username, lab: 'Логин', sec: false },
    { k: 'Пароль', v: c.password, lab: 'Пароль', sec: true },
  ]
})
</script>

<template>
  <ClientsModal
    v-model:open="open"
    :title="title"
    :wide="kind === 'ike' || (kind === 'wg' && qrWide)"
    closable
  >
    <template v-if="kind === 'wg'">
      <p class="mt">
        Отсканируй QR в приложении WireGuard или скачай .conf и импортируй.
      </p>
      <div v-if="hasAAAA" class="al amb">
        Уберите у домена IPv6-запись (AAAA), иначе у клиентов на Linux VPN может не заработать.
      </div>
      <img
        :src="qrSrc"
        alt="WireGuard QR"
        class="qr"
        @load="qrWide = ($event.target as HTMLImageElement).naturalWidth > 388"
      >
    </template>

    <template v-else-if="kind === 'ovpn'">
      <p class="mt">
        .ovpn-файл с встроенными сертификатами — импортируй в OpenVPN Connect или openvpn3.
      </p>
      <div v-if="loading" class="wait">
        генерирую сертификат…
      </div>
      <div v-else class="kvr">
        <span>Файл</span><code>{{ fileName }}</code><span />
      </div>
    </template>

    <template v-else>
      <p class="mt">
        Нативный IKEv2 в iOS / macOS / Android / Windows. Введи поля в настройках VPN системы<template v-if="creds?.hasCa"> и установи CA-сертификат</template>.
      </p>
      <div v-if="loading" class="wait">
        генерирую креды…
      </div>
      <template v-else-if="creds">
        <div v-if="creds.certificatePending" class="al amb">
          <b>Ожидается сертификат Caddy</b>
          IKEv2 автоматически перейдёт на домен после выдачи сертификата. Пока действуют временные настройки ниже. После перехода откройте это окно заново.
        </div>
        <div class="kvl">
          <div v-for="r in ikeRows" :key="r.lab" class="kvr">
            <span>{{ r.k }}</span>
            <code>{{ r.sec && !showPass ? '•'.repeat(20) : r.v }}</code>
            <span class="r2">
              <button
                v-if="r.sec"
                type="button"
                class="xb"
                aria-label="Показать"
                :aria-pressed="showPass"
                @click="showPass = !showPass"
              >
                <svg class="i" viewBox="0 0 24 24"><path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6S2 12 2 12z" /><circle cx="12" cy="12" r="2.6" /></svg>
              </button>
              <button
                type="button"
                class="xb"
                aria-label="Копировать"
                :style="copied === r.lab ? 'color:var(--green)' : undefined"
                @click="copyField(r.lab, r.v, $event.currentTarget as HTMLElement)"
              >
                <svg v-if="copied === r.lab" class="i" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
                <svg v-else class="i" viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2.5" /><path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8" /></svg>
              </button>
            </span>
          </div>
        </div>
      </template>
    </template>

    <div class="mf c3">
      <template v-if="kind === 'ike'">
        <button type="button" class="pill" :disabled="!creds" @click="copyAll">
          Копировать
        </button>
        <a
          v-if="creds?.hasCa"
          class="pill"
          :href="ikev2CaCrtUrl()"
          target="_blank"
          rel="noopener"
        >Скачать CA</a>
      </template>
      <template v-else>
        <button type="button" class="pill" :disabled="!conf" @click="copyConf">
          Копировать
        </button>
        <button type="button" class="pill" :disabled="!conf" @click="download">
          Скачать
        </button>
      </template>
      <span :title="canSendTg ? undefined : tgReason">
        <button
          type="button"
          class="pill w"
          :disabled="!canSendTg || sending || (kind === 'ike' ? !creds : !conf)"
          @click="sendTg"
        >
          В TG
        </button>
      </span>
    </div>
  </ClientsModal>
</template>
