<script setup lang="ts">
import QRCode from 'qrcode-svg'

const { user, fetch: refreshSession } = useAuthSession()
const { toast, busy: keys, act } = useAct()

const totpEnabled = computed(() => user.value?.twoFactorEnabled ?? false)

// Настройка идёт, пока сервер выдал адрес секрета и первый код ещё не принят.
const totpUri = ref('')
const totpSecret = computed(() => totpUri.value ? new URL(totpUri.value).searchParams.get('secret') ?? '' : '')
const pwd = ref('')
const code = ref('')
const busy = computed(() => keys.value.has('2fa'))
const disableOpen = ref(false)

// QR: 4 px на модуль с тихой зоной 4 модуля, ecl L.
const totpQr = computed(() => {
  if (!totpUri.value) return null
  const { modules } = new QRCode({ content: totpUri.value, ecl: 'L' }).qrcode
  const dark = modules.flatMap((row, y) => row.flatMap((on, x) => on ? [{ x: x + 4, y: y + 4 }] : []))
  return { size: modules.length + 8, dark }
})

function reset() {
  totpUri.value = pwd.value = code.value = ''
}
watch(disableOpen, reset)

function call<T>(path: string, body: object, fail: string, done: (r: T) => unknown) {
  return act('2fa', async () => done(await $fetch<unknown>(`/api/auth/two-factor/${path}`, { method: 'POST', body }) as T), undefined, { title: fail })
}

// Код из аутентификатора: 6 цифр, пробелы и прочее не в счёт; без пароля код не принимается.
function typedCode(passwordGiven = true) {
  if (passwordGiven && code.value.replace(/\D/g, '').length === 6) return code.value.replace(/\s/g, '')
  toast.add({ title: 'Код не подошёл', color: 'error' })
}

function startSetup() {
  if (!pwd.value) return toast.add({ title: 'Введите текущий пароль', color: 'error' })
  return call<{ totpURI: string }>('enable', { password: pwd.value }, 'Ошибка setup', (r) => {
    totpUri.value = r.totpURI
    pwd.value = ''
  })
}

// Первый код включает 2FA; остальные сессии сервер рвёт.
function confirmSetup() {
  const c = typedCode()
  if (c) return call('verify-totp', { code: c }, 'Неверный код', () => finish('2FA включена'))
}

function disableTotp() {
  const c = typedCode(!!pwd.value)
  if (c) return call('disable', { password: pwd.value, code: c }, 'Ошибка', () => finish('2FA выключена'))
}

async function finish(title: string) {
  await refreshSession()
  toast.add({ title, color: 'success' })
  disableOpen.value = false
  reset()
}
</script>

<template>
  <div id="tfa">
    <div class="sh">
      <h3>Двухфакторный вход</h3>
      <span class="tag" :class="{ ok: totpEnabled }">{{ totpEnabled ? 'Включена' : 'Выключена' }}</span>
    </div>
    <template v-if="totpEnabled">
      <p class="mt">
        2FA активна. Для отключения подтверди пароль и код из аутентификатора.
      </p>
      <div class="fld">
        <span>Аутентификатор</span><code />
        <button type="button" class="pill red" @click="disableOpen = true">
          Выключить 2FA
        </button>
      </div>
    </template>
    <form v-else-if="!totpUri" @submit.prevent="startSetup">
      <p class="mt">
        При входе панель спросит 6-значный код из приложения-аутентификатора (Aegis, Google Authenticator, 1Password) при входе.
      </p>
      <label class="fld">
        <span>Текущий пароль</span>
        <input v-model="pwd" class="in" type="password" autocomplete="current-password">
      </label>
      <div class="fld">
        <span /><code />
        <button type="submit" class="pill w" :disabled="busy">
          Включить 2FA
        </button>
      </div>
    </form>
    <form v-else @submit.prevent="confirmSetup">
      <p class="mt">
        Отсканируй QR в аутентификаторе или вставь секрет вручную:
      </p>
      <div class="qrrow">
        <div class="qr">
          <svg v-if="totpQr" xmlns="http://www.w3.org/2000/svg" :viewBox="`0 0 ${totpQr.size} ${totpQr.size}`" :style="{ width: `${totpQr.size * 4}px` }" shape-rendering="crispEdges">
            <rect :width="totpQr.size" :height="totpQr.size" fill="#fff" />
            <rect v-for="m in totpQr.dark" :key="`${m.x}-${m.y}`" :x="m.x" :y="m.y" width="1" height="1" fill="#000" />
          </svg>
        </div>
        <input class="in mono" readonly :value="totpSecret" aria-label="Секрет">
      </div>
      <label class="fld qrcode">
        <span>Введи 6-значный код из приложения</span>
        <input v-model="code" class="in mono" placeholder="123 456" inputmode="numeric" autocomplete="one-time-code" autofocus>
      </label>
      <div class="fld">
        <span /><code />
        <button type="button" class="pill" @click="reset">
          Отмена
        </button>
        <button type="submit" class="pill w" :disabled="busy">
          Подтвердить
        </button>
      </div>
    </form>
  </div>

  <PanelDialog v-model:open="disableOpen" title="Выключить 2FA?" ok="Выключить" danger :busy="busy" @ok="disableTotp">
    <label class="fl2"><span>Пароль</span><input v-model="pwd" class="in" type="password" autocomplete="current-password" @keydown.enter="disableTotp"></label>
    <label class="fl2">
      <span>Код 2FA (6 цифр)</span><input v-model="code" class="in mono" placeholder="123 456" inputmode="numeric" autocomplete="one-time-code" @keydown.enter="disableTotp">
    </label>
  </PanelDialog>
</template>
