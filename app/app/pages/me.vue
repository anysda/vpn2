<script setup lang="ts">
import QRCode from 'qrcode-svg'

useHead({ title: 'Профиль — anysda-vpn2' })

const { user, loginMethod, fetch: refreshSession } = useAuthSession()
const toast = useToast()
const { sso, load: loadSso } = useSsoPublic()

const viaSso = computed(() => loginMethod.value === 'sso')

// ---------------------- Password change ----------------------
const pwd = reactive({ current: '', next: '', confirm: '' })
const pwdLoading = ref(false)

async function changePassword() {
  if (!pwd.current || !pwd.next) return
  if (pwd.next !== pwd.confirm) {
    toast.add({ title: 'Пароли не совпадают', color: 'error' })
    return
  }
  if (pwd.next.length < 8) {
    toast.add({ title: 'Пароль должен быть ≥ 8 символов', color: 'error' })
    return
  }
  pwdLoading.value = true
  try {
    // Остальные сессии сервер рвёт всегда, эта остаётся.
    await $fetch('/api/auth/change-password', {
      method: 'POST',
      body: { currentPassword: pwd.current, newPassword: pwd.next },
    })
    toast.add({ title: 'Пароль обновлён', color: 'success' })
    pwd.current = pwd.next = pwd.confirm = ''
  }
  catch (e) {
    const err = e as { statusMessage?: string, message?: string }
    toast.add({ title: 'Ошибка', color: 'error', description: err.statusMessage ?? err.message })
  }
  finally {
    pwdLoading.value = false
  }
}

// ---------------------- TOTP ----------------------
const totpEnabled = computed(() => user.value?.twoFactorEnabled ?? false)

const setupState = ref<'idle' | 'setup' | 'confirming'>('idle')
const totpSecret = ref('')
const totpUri = ref('')
const totpCode = ref('')
const setupPwd = ref('')
const totpLoading = ref(false)

const totpQrSvg = computed(() => {
  if (!totpUri.value) return ''
  return new QRCode({ content: totpUri.value, width: 192, height: 192, padding: 4, ecl: 'M', swap: true }).svg()
})

async function startSetup() {
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

async function confirmSetup() {
  if (!totpCode.value.trim()) return
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
  if (!disablePwd.value || !disableTotpCode.value) return
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
// ---------------------- Вход через Authentik ----------------------
interface SsoForm {
  enabled: boolean
  authentikUrl: string
  appSlug: string
  clientId: string
  clientSecret: string
  allowedSubs: string
  label: string
  autoRedirect: boolean
  passwordLogin: boolean
  redirectUrl: string
}
const ssoForm = reactive<SsoForm>({
  enabled: false, authentikUrl: '', appSlug: '', clientId: '', clientSecret: '',
  allowedSubs: '', label: 'Authentik', autoRedirect: true, passwordLogin: true, redirectUrl: '',
})
const ssoSecretMasked = ref('')
const ssoSaving = ref(false)
const ssoChecking = ref(false)
const ssoCheck = ref<{ ok: boolean, text: string } | null>(null)
const ssoLoaded = ref(false)
const ssoLoading = ref(false)
const ssoLoadError = ref('')

async function loadSsoForm() {
  const r = await $fetch<Omit<SsoForm, 'clientSecret'> & { clientSecretMasked: string }>('/api/admin/sso')
  const { clientSecretMasked, ...rest } = r
  Object.assign(ssoForm, rest, { clientSecret: '' })
  ssoSecretMasked.value = clientSecretMasked
}
async function reloadSsoForm() {
  if (ssoLoading.value) return
  ssoLoading.value = true
  ssoLoaded.value = false
  ssoLoadError.value = ''
  try {
    await loadSsoForm()
    ssoLoaded.value = true
  }
  catch (e) {
    ssoLoadError.value = apiError(e)
  }
  finally {
    ssoLoading.value = false
  }
}
onMounted(reloadSsoForm)

function apiError(e: unknown): string {
  const err = e as { data?: { message?: string }, statusMessage?: string, message?: string }
  return err.data?.message ?? err.statusMessage ?? err.message ?? 'Ошибка'
}

async function checkSso() {
  ssoChecking.value = true
  ssoCheck.value = null
  try {
    const r = await $fetch<{ issuer: string }>('/api/admin/sso/check', {
      method: 'POST',
      body: { authentikUrl: ssoForm.authentikUrl, appSlug: ssoForm.appSlug },
    })
    ssoCheck.value = { ok: true, text: `Discovery отвечает, issuer: ${r.issuer}` }
  }
  catch (e) {
    ssoCheck.value = { ok: false, text: apiError(e) }
  }
  finally {
    ssoChecking.value = false
  }
}

async function saveSso() {
  if (!ssoLoaded.value || ssoSaving.value) return
  ssoSaving.value = true
  try {
    await $fetch('/api/admin/sso', { method: 'PUT', body: ssoForm })
    toast.add({ title: 'Настройки входа сохранены', color: 'success' })
    await Promise.all([reloadSsoForm(), loadSso()])
  }
  catch (e) {
    toast.add({ title: 'Не сохранено', color: 'error', description: apiError(e) })
  }
  finally {
    ssoSaving.value = false
  }
}
</script>

<template>
  <div class="max-w-xl mx-auto">
    <h1 class="text-2xl font-semibold mb-6">
      Мой профиль
    </h1>

    <UCard class="mb-6">
      <template #header>
        <div class="font-medium">
          Логин
        </div>
      </template>
      <div class="text-sm flex items-center gap-2">
        {{ user?.username ?? '—' }}
        <UBadge v-if="viaSso" color="primary" variant="subtle" size="sm">
          вход через {{ sso.label }}
        </UBadge>
      </div>
    </UCard>

    <UCard class="mb-6">
      <template #header>
        <div class="font-medium">
          Смена пароля
        </div>
      </template>
      <UAlert
        v-if="sso.enabled"
        class="mb-4"
        color="neutral"
        variant="soft"
        icon="i-lucide-life-buoy"
        title="Пароль — аварийный вход"
        description="Форма входа по паролю открывается по адресу /login?direct=1, если включено «Оставить вход по паролю». При выключенной настройке аварийный вход тоже недоступен."
      />
      <form class="space-y-3" @submit.prevent="changePassword">
        <UFormField label="Текущий пароль">
          <UInput v-model="pwd.current" type="password" autocomplete="current-password" class="w-full" />
        </UFormField>
        <UFormField label="Новый пароль (≥ 8 символов)">
          <UInput v-model="pwd.next" type="password" autocomplete="new-password" class="w-full" />
        </UFormField>
        <UFormField label="Повторите">
          <UInput v-model="pwd.confirm" type="password" autocomplete="new-password" class="w-full" />
        </UFormField>
        <div class="flex justify-end">
          <UButton
            type="submit"
            :loading="pwdLoading"
            :disabled="!pwd.current || !pwd.next || pwd.next !== pwd.confirm"
          >
            Сохранить пароль
          </UButton>
        </div>
      </form>
    </UCard>

    <UCard>
      <template #header>
        <div class="font-medium flex items-center gap-2">
          Двухфакторная аутентификация
          <UBadge
            :color="totpEnabled ? 'success' : 'neutral'"
            :variant="totpEnabled ? 'subtle' : 'soft'"
            size="sm"
          >
            {{ totpEnabled ? 'Включена' : 'Выключена' }}
          </UBadge>
        </div>
      </template>

      <div v-if="!totpEnabled && setupState === 'idle'">
        <p class="text-sm text-(--ui-text-muted) mb-3">
          Добавьте код из приложения-аутентификатора (Aegis, Google Authenticator, 1Password) при входе.
        </p>
        <form class="flex flex-wrap gap-2" @submit.prevent="startSetup">
          <UInput
            v-model="setupPwd"
            type="password"
            autocomplete="current-password"
            placeholder="Текущий пароль"
            class="flex-1 min-w-48"
          />
          <UButton
            type="submit"
            color="primary"
            variant="soft"
            icon="i-lucide-shield-plus"
            :loading="totpLoading"
            :disabled="!setupPwd"
          >
            Включить 2FA
          </UButton>
        </form>
      </div>

      <div v-else-if="!totpEnabled && setupState === 'setup'" class="space-y-4">
        <p class="text-sm">
          Отсканируй QR в аутентификаторе или вставь секрет вручную:
        </p>
        <div class="flex justify-center bg-white p-3 rounded-md" v-html="totpQrSvg" />
        <UInput :model-value="totpSecret" readonly class="font-mono text-xs" />
        <UFormField label="Введи 6-значный код из приложения">
          <UInput
            v-model="totpCode"
            placeholder="123 456"
            inputmode="numeric"
            autocomplete="one-time-code"
            class="w-full"
            autofocus
          />
        </UFormField>
        <div class="flex justify-end gap-2">
          <UButton color="neutral" variant="soft" @click="cancelSetup">
            Отмена
          </UButton>
          <UButton
            color="primary"
            :loading="totpLoading"
            :disabled="!totpCode.trim()"
            @click="confirmSetup"
          >
            Подтвердить
          </UButton>
        </div>
      </div>

      <div v-else class="space-y-3">
        <p class="text-sm text-(--ui-text-muted)">
          2FA активна. Для отключения подтверди пароль и код из аутентификатора.
        </p>
        <UButton
          color="error"
          variant="soft"
          icon="i-lucide-shield-off"
          @click="() => { disableConfirm = true }"
        >
          Выключить 2FA
        </UButton>
      </div>
    </UCard>

    <UCard class="mt-6">
      <template #header>
        <div class="font-medium flex items-center gap-2">
          Вход через Authentik
          <UBadge :color="sso.enabled ? 'success' : 'neutral'" :variant="sso.enabled ? 'subtle' : 'soft'" size="sm">
            {{ sso.enabled ? 'Включён' : 'Выключен' }}
          </UBadge>
        </div>
      </template>
      <UAlert
        v-if="ssoLoadError"
        role="alert"
        color="error"
        variant="soft"
        title="Не удалось загрузить настройки входа"
        :description="ssoLoadError"
        class="mb-3"
      />
      <UButton v-if="ssoLoadError" class="mb-3" :loading="ssoLoading" @click="reloadSsoForm">
        Повторить загрузку
      </UButton>
      <form @submit.prevent="saveSso">
        <fieldset :disabled="!ssoLoaded || ssoSaving" class="space-y-3">
          <div class="flex items-center justify-between gap-3">
            <span class="text-sm">Вход через Authentik</span>
            <USwitch v-model="ssoForm.enabled" aria-label="Вход через Authentik" />
          </div>
          <UFormField label="Адрес Authentik или discovery URL">
            <UInput v-model="ssoForm.authentikUrl" placeholder="https://auth.example.com" class="w-full" />
          </UFormField>
          <UFormField label="Slug приложения" hint="не нужен, если выше discovery URL">
            <UInput v-model="ssoForm.appSlug" placeholder="vpn2" class="w-full" />
          </UFormField>
          <div class="flex flex-wrap items-center gap-2">
            <UButton
              color="neutral"
              variant="soft"
              icon="i-lucide-plug-zap"
              :loading="ssoChecking"
              :disabled="!ssoForm.authentikUrl"
              @click="checkSso"
            >
              Проверить
            </UButton>
            <span v-if="ssoCheck" class="text-sm" :class="ssoCheck.ok ? 'text-(--ui-success)' : 'text-(--ui-error)'">
              {{ ssoCheck.text }}
            </span>
          </div>
          <UFormField label="client_id">
            <UInput v-model="ssoForm.clientId" class="w-full" autocomplete="off" />
          </UFormField>
          <UFormField label="client_secret" :hint="ssoSecretMasked ? `сохранён: ${ssoSecretMasked}` : undefined">
            <UInput
              v-model="ssoForm.clientSecret"
              type="password"
              autocomplete="new-password"
              :placeholder="ssoSecretMasked ? 'оставить прежний' : ''"
              class="w-full"
            />
          </UFormField>
          <UFormField label="Кого пускать" help="uuid пользователя Authentik (sub), через запятую; с логином панели: uuid:логин">
            <UInput v-model="ssoForm.allowedSubs" class="w-full font-mono text-xs" />
          </UFormField>
          <UFormField label="Адрес возврата" help="Его же впишите в Redirect URI провайдера в Authentik">
            <UInput v-model="ssoForm.redirectUrl" class="w-full font-mono text-xs" />
          </UFormField>
          <UFormField label="Подпись кнопки">
            <UInput v-model="ssoForm.label" placeholder="Authentik" class="w-full" />
          </UFormField>
          <div class="flex items-center justify-between gap-3">
            <span class="text-sm">Бесшовный вход: сразу уводить в Authentik</span>
            <USwitch v-model="ssoForm.autoRedirect" aria-label="Бесшовный вход" />
          </div>
          <div class="flex items-center justify-between gap-3">
            <span class="text-sm">Оставить вход по паролю</span>
            <USwitch v-model="ssoForm.passwordLogin" aria-label="Оставить вход по паролю" />
          </div>
          <p v-if="!ssoForm.passwordLogin" class="text-xs text-(--ui-text-muted)">
            Без пароля аварийного входа /login?direct=1 нет. Выключается отдельным сохранением, после входа через Authentik.
          </p>
          <div class="flex justify-end">
            <UButton type="submit" :loading="ssoSaving">
              Сохранить
            </UButton>
          </div>
        </fieldset>
      </form>
    </UCard>

    <UModal v-model:open="disableConfirm" title="Выключить 2FA?" :ui="{ content: 'max-w-md' }">
      <template #body>
        <div class="space-y-3">
          <p class="text-sm">
            Введите текущий пароль и код из аутентификатора.
          </p>
          <UInput
            v-model="disablePwd"
            type="password"
            autocomplete="current-password"
            placeholder="Пароль"
            class="w-full"
          />
          <UInput
            v-model="disableTotpCode"
            inputmode="numeric"
            autocomplete="one-time-code"
            placeholder="Код 2FA (6 цифр)"
            class="w-full"
          />
        </div>
      </template>
      <template #footer>
        <UButton color="neutral" variant="soft" @click="() => { disableConfirm = false }">
          Отмена
        </UButton>
        <UButton
          color="error"
          :loading="disableLoading"
          :disabled="!disablePwd || !disableTotpCode"
          @click="disableTotp"
        >
          Выключить
        </UButton>
      </template>
    </UModal>
  </div>
</template>
