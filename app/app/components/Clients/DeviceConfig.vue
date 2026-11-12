<script setup lang="ts">
import type { Device } from '~/composables/useClients'

type Kind = 'wg' | 'ovpn' | 'ike'
type Ike = Api<'/api/clients/:id/devices/:deviceId/ikev2-credentials'>

const props = defineProps<{ clientId: number, device: Device, kind: Kind }>()
const open = defineModel<boolean>('open', { default: false })
const { canSend, reason: tgReason } = useTelegramStatus()
const { toast, busy, act } = useAct()

const KINDS = {
  wg: { name: 'WireGuard', get: 'wg-config', send: 'send-wg-to-tg', sent: 'WireGuard-конфиг отправлен в Telegram', fail: 'Не удалось получить .conf' },
  ovpn: { name: 'OpenVPN', get: 'ovpn-config', send: 'send-ovpn-to-tg', sent: 'OpenVPN-конфиг отправлен в Telegram', fail: 'Не удалось получить .ovpn' },
  ike: { name: 'IKEv2', get: 'ikev2-credentials', send: 'send-ikev2-to-tg', sent: 'IKEv2 отправлен в Telegram', fail: 'Не удалось получить IKEv2-креды' },
}
const k = computed(() => KINDS[props.kind])
const base = computed(() => `/api/clients/${props.clientId}/devices/${props.device.id}`)
const ext = computed(() => props.kind === 'wg' ? 'conf' : 'ovpn')
const fileName = computed(() => `${props.device.configName}.${ext.value}`)

const conf = ref('')
const creds = ref<Ike | null>(null)
const hasAAAA = ref(false)
const qr = ref('')
// Окно вмещает QR до 97 модулей по 4 px с тихой зоной: крупнее - окно шире.
const qrWide = ref(false)
const showPass = ref(false)
let request = 0

async function load() {
  const r = ++request
  conf.value = ''
  creds.value = null
  hasAAAA.value = showPass.value = qrWide.value = false
  const url = `${base.value}/${k.value.get}`
  if (props.kind === 'wg') {
    // Без метки браузер показал бы из кэша QR старого ключа после перевыпуска.
    qr.value = `${base.value}/wg-qrcode.svg?t=${Date.now()}`
    void $fetch('/api/wg-endpoint-warning', { timeout: 1000, retry: 0 }).then(w => r === request && (hasAAAA.value = w.hasAAAA), () => {})
  }
  await act('load', async () => {
    const res = props.kind === 'ike' ? await $fetch<Ike>(url) : await $fetch<string>(url, { responseType: 'text' })
    if (r !== request) return
    if (typeof res === 'string') conf.value = res
    else creds.value = res
  }, undefined, k.value.fail)
}
watch(open, now => now ? void load() : request++, { immediate: true })

async function copy(text: string, done: string) {
  toast.add(await copyText(text) ? { title: done, color: 'success' } : { title: 'Не удалось скопировать', color: 'error' })
}
function download() {
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([conf.value], { type: props.kind === 'wg' ? 'text/plain' : 'application/x-openvpn-profile' }))
  a.download = fileName.value
  a.click()
  URL.revokeObjectURL(a.href)
  toast.add({ title: `Скачивается ${fileName.value}`, color: 'success' })
}
const sendTg = () => act('tg', () => $fetch<unknown>(`${base.value}/${k.value.send}`, { method: 'POST' }), { title: k.value.sent, color: 'success' }, 'Ошибка отправки')

const IKE_ROWS = [
  { k: 'Remote ID (iOS) / Server identity (Android)', field: 'remoteId', lab: 'Remote ID', secret: false },
  { k: 'Сервер', field: 'server', lab: 'Сервер', secret: false },
  { k: 'Логин', field: 'username', lab: 'Логин', secret: false },
  { k: 'Пароль', field: 'password', lab: 'Пароль', secret: true },
] as const
const ikeText = computed(() => creds.value ? `Сервер: ${creds.value.server}\nRemote ID: ${creds.value.remoteId}\nЛогин: ${creds.value.username}\nПароль: ${creds.value.password}` : '')
</script>

<template>
  <PanelDialog v-model:open="open" :title="`${KINDS[kind].name} · ${device.name}`" :wide="kind === 'ike' || qrWide">
    <template v-if="kind === 'wg'">
      <p class="mt">Отсканируй QR в приложении WireGuard или скачай .conf и импортируй.</p>
      <div v-if="hasAAAA" class="al amb">
        Уберите у домена IPv6-запись (AAAA), иначе у клиентов на Linux VPN может не заработать.
      </div>
      <img :src="qr" alt="WireGuard QR" class="qr" @load="qrWide = ($event.target as HTMLImageElement).naturalWidth > 388">
    </template>
    <template v-else-if="kind === 'ovpn'">
      <p class="mt">
        .ovpn-файл с встроенными сертификатами — импортируй в OpenVPN Connect или openvpn3.
      </p>
      <div v-if="busy.has('load')" class="wait">генерирую сертификат…</div>
      <div v-else class="kvr">
        <span>Файл</span><code>{{ fileName }}</code><span />
      </div>
    </template>
    <template v-else>
      <p class="mt">
        Нативный IKEv2 в iOS / macOS / Android / Windows. Введи поля в настройках VPN системы<template v-if="creds?.caCertPem"> и установи CA-сертификат</template>.
      </p>
      <div v-if="busy.has('load')" class="wait">генерирую креды…</div>
      <div v-if="creds?.certificatePending" class="al amb">
        <b>Ожидается сертификат Caddy</b>
        IKEv2 автоматически перейдёт на домен после выдачи сертификата. Пока действуют временные настройки ниже. После перехода откройте это окно заново.
      </div>
      <div v-if="creds" class="kvl">
        <div v-for="r in IKE_ROWS" :key="r.lab" class="kvr">
          <span>{{ r.k }}</span>
          <code>{{ r.secret && !showPass ? '•'.repeat(20) : creds[r.field] }}</code>
          <span class="r2">
            <button v-if="r.secret" type="button" class="xb" aria-label="Показать" :aria-pressed="showPass" @click="showPass = !showPass">
              <svg class="i" viewBox="0 0 24 24"><path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6S2 12 2 12z" /><circle cx="12" cy="12" r="2.6" /></svg>
            </button>
            <button type="button" class="xb" aria-label="Копировать" @click="copy(creds[r.field], `${r.lab} скопирован`)">
              <svg class="i" viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2.5" /><path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8" /></svg>
            </button>
          </span>
        </div>
      </div>
    </template>

    <template #footer>
      <template v-if="kind === 'ike'">
        <button type="button" class="pill" :disabled="!creds" @click="copy(ikeText, 'IKEv2 креды скопированы')">
          Копировать
        </button>
        <a v-if="creds?.caCertPem" class="pill" href="/api/ikev2/ca.crt" target="_blank" rel="noopener">Скачать CA</a>
      </template>
      <template v-else>
        <button type="button" class="pill" :disabled="!conf" @click="copy(conf, `.${ext} скопирован`)">
          Копировать
        </button>
        <button type="button" class="pill" :disabled="!conf" @click="download">Скачать</button>
      </template>
      <span :title="tgReason || undefined">
        <button type="button" class="pill w" :disabled="!canSend || busy.has('tg') || (kind === 'ike' ? !creds : !conf)" @click="sendTg">
          В TG
        </button>
      </span>
    </template>
  </PanelDialog>
</template>
