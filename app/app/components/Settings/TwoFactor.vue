<script setup lang="ts">
import QRCode from 'qrcode-svg'

const { user, fetch: refreshSession } = useAuthSession()
const toast = useToast()
// Телефон: окно «Выключить 2FA?» — нижней шторкой вместо модалки.
const mobile = useMobile()

const totpEnabled = computed(() => user.value?.twoFactorEnabled ?? false)

const setupState = ref<'idle' | 'setup' | 'confirming'>('idle')
const totpSecret = ref('')
const totpUri = ref('')
const totpCode = ref('')
const setupPwd = ref('')
const totpLoading = ref(false)

// QR разметкой: квадрат на каждый тёмный модуль, тихая зона 4 модуля, 4 px на модуль
// (меньше камера с экрана не берёт); ecl L - версия ниже, модуль крупнее.
const totpQr = computed(() => {
  if (!totpUri.value) return null
  const { modules } = new QRCode({ content: totpUri.value, ecl: 'L' }).qrcode
  const dark = modules.flatMap((row, y) => row.flatMap((on, x) => on ? [{ x: x + 4, y: y + 4 }] : []))
  return { size: modules.length + 8, dark }
})

async function startSetup() {
  if (!setupPwd.value) {
    toast.add({ title: 'Введите текущий пароль', color: 'error' })
    return
  }
  totpLoading.value = true
  try {
    const r = await $fetch<{ totpURI: string }>('/api/auth/two-factor/enable', {
      method: 'POST',
      body: { password: setupPwd.value },
    })
    totpSecret.value = new URL(r.totpURI).searchParams.get('secret') ?? ''
    totpUri.value = r.totpURI
    setupPwd.value = ''
    setupState.value = 'setup'
  }
  catch (e) {
    const err = e as { statusMessage?: string, message?: string }
    toast.add({ title: 'Ошибка setup', color: 'error', description: err.statusMessage ?? err.message })
  }
  finally {
    totpLoading.value = false
  }
}

// Код из аутентификатора: 6 цифр, пробелы и прочее не в счёт.
const sixDigits = (v: string) => v.replace(/\D/g, '').length === 6

async function confirmSetup() {
  if (!sixDigits(totpCode.value)) {
    toast.add({ title: 'Код не подошёл', color: 'error' })
    return
  }
  totpLoading.value = true
  try {
    // Первый код включает 2FA; остальные сессии сервер рвёт.
    await $fetch('/api/auth/two-factor/verify-totp', {
      method: 'POST',
      body: { code: totpCode.value.replace(/\s/g, '') },
    })
    await refreshSession()
    toast.add({ title: '2FA включена', color: 'success' })
    setupState.value = 'idle'
    totpSecret.value = ''
    totpUri.value = ''
    totpCode.value = ''
  }
  catch (e) {
    const err = e as { statusMessage?: string, message?: string }
    toast.add({ title: 'Неверный код', color: 'error', description: err.statusMessage ?? err.message })
  }
  finally {
    totpLoading.value = false
  }
}

function cancelSetup() {
  setupState.value = 'idle'
  totpSecret.value = ''
  totpUri.value = ''
  totpCode.value = ''
}

const disableConfirm = ref(false)
const disablePwd = ref('')
const disableTotpCode = ref('')
const disableLoading = ref(false)

async function disableTotp() {
  if (!disablePwd.value || !sixDigits(disableTotpCode.value)) {
    toast.add({ title: 'Код не подошёл', color: 'error' })
    return
  }
  disableLoading.value = true
  try {
    await $fetch('/api/auth/two-factor/disable', {
      method: 'POST',
      body: { password: disablePwd.value, code: disableTotpCode.value.replace(/\s/g, '') },
    })
    await refreshSession()
    toast.add({ title: '2FA выключена', color: 'success' })
    disableConfirm.value = false
    disablePwd.value = ''
    disableTotpCode.value = ''
  }
  catch (e) {
    const err = e as { statusMessage?: string, message?: string }
    toast.add({ title: 'Ошибка', color: 'error', description: err.statusMessage ?? err.message })
  }
  finally {
    disableLoading.value = false
  }
}
</script>

<template>
  <div id="tfa">
    <div class="sh">
      <h3>Двухфакторный вход</h3>
      <span v-if="totpEnabled" class="tag ok">Включена</span>
      <span v-else class="tag">Выключена</span>
    </div>
    <template v-if="totpEnabled">
      <p class="mt">2FA активна. Для отключения подтверди пароль и код из аутентификатора.</p>
      <div class="fld">
        <span>Аутентификатор</span><code />
        <button type="button" class="pill red" @click="disableConfirm = true">Выключить 2FA</button>
      </div>
    </template>
    <form v-else-if="setupState === 'idle'" @submit.prevent="startSetup">
      <p class="mt">При входе панель спросит 6-значный код из приложения-аутентификатора (Aegis, Google Authenticator, 1Password) при входе.</p>
      <label class="fld"><span>Текущий пароль</span><input v-model="setupPwd" class="in" type="password" autocomplete="current-password"></label>
      <div class="fld">
        <span /><code />
        <button type="submit" class="pill w" :disabled="totpLoading">Включить 2FA</button>
      </div>
    </form>
    <form v-else @submit.prevent="confirmSetup">
      <p class="mt">Отсканируй QR в аутентификаторе или вставь секрет вручную:</p>
      <div class="qrrow">
        <div class="qr">
          <svg v-if="totpQr" xmlns="http://www.w3.org/2000/svg" :viewBox="`0 0 ${totpQr.size} ${totpQr.size}`" :style="{ width: `${totpQr.size * 4}px` }" shape-rendering="crispEdges">
            <rect :width="totpQr.size" :height="totpQr.size" fill="#fff" />
            <rect v-for="m in totpQr.dark" :key="`${m.x}-${m.y}`" :x="m.x" :y="m.y" width="1" height="1" fill="#000" />
          </svg>
        </div>
        <input class="in mono" readonly :value="totpSecret" aria-label="Секрет">
      </div>
      <label class="fld qrcode"><span>Введи 6-значный код из приложения</span><input
        v-model="totpCode"
        class="in mono"
        placeholder="123 456"
        inputmode="numeric"
        autocomplete="one-time-code"
        autofocus
      ></label>
      <div class="fld">
        <span /><code />
        <button type="button" class="pill" @click="cancelSetup">Отмена</button>
        <button type="submit" class="pill w" :disabled="totpLoading">Подтвердить</button>
      </div>
    </form>
  </div>

  <UModal
    v-if="!mobile"
    v-model:open="disableConfirm"
    title="Выключить 2FA?"
    :ui="{ overlay: 'stg-ovl', content: 'card stg-modal' }"
  >
    <template #content>
      <form @submit.prevent="disableTotp">
        <div class="mh">
          <h2>Выключить 2FA?</h2>
          <button type="button" class="xb" aria-label="Закрыть" @click="disableConfirm = false"><svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg></button>
        </div>
        <label class="fl2"><span>Пароль</span><input v-model="disablePwd" class="in" type="password" autocomplete="current-password"></label>
        <label class="fl2"><span>Код 2FA (6 цифр)</span><input
          v-model="disableTotpCode"
          class="in mono"
          placeholder="123 456"
          inputmode="numeric"
          autocomplete="one-time-code"
        ></label>
        <div class="mf">
          <button type="button" class="pill" @click="disableConfirm = false">Отмена</button>
          <button type="submit" class="pill redw" :disabled="disableLoading">Выключить</button>
        </div>
      </form>
    </template>
  </UModal>
  <BottomSheet v-else :open="disableConfirm" label="Выключить 2FA?" @close="disableConfirm = false">
    <form class="stg-sheet" @submit.prevent="disableTotp">
      <div class="mh">
        <h2>Выключить 2FA?</h2>
        <button type="button" class="xb" aria-label="Закрыть" @click="disableConfirm = false"><svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg></button>
      </div>
      <label class="fl2"><span>Пароль</span><input v-model="disablePwd" class="in" type="password" autocomplete="current-password"></label>
      <label class="fl2"><span>Код 2FA (6 цифр)</span><input
        v-model="disableTotpCode"
        class="in mono"
        placeholder="123 456"
        inputmode="numeric"
        autocomplete="one-time-code"
      ></label>
      <div class="bsf">
        <button type="button" class="pill" @click="disableConfirm = false">Отмена</button>
        <button type="submit" class="pill redw" :disabled="disableLoading">Выключить</button>
      </div>
    </form>
  </BottomSheet>
</template>
