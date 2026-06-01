<script setup lang="ts">
import QRCode from 'qrcode-svg'

useHead({ title: 'Профиль — anysda-vpn2' })

const { user, fetch: refreshSession } = useUserSession()
const toast = useToast()

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
    await $fetch('/api/auth/password', {
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
const totpEnabled = computed(() => user.value?.totpEnabled ?? false)

const setupState = ref<'idle' | 'setup' | 'confirming'>('idle')
const totpSecret = ref('')
const totpUri = ref('')
const totpCode = ref('')
const totpLoading = ref(false)

const totpQrSvg = computed(() => {
  if (!totpUri.value) return ''
  return new QRCode({ content: totpUri.value, width: 192, height: 192, padding: 2, ecl: 'M' }).svg()
})

async function startSetup() {
  totpLoading.value = true
  try {
    const r = await $fetch<{ secret: string, otpauthUrl: string }>('/api/auth/totp/setup', {
      method: 'POST',
    })
    totpSecret.value = r.secret
    totpUri.value = r.otpauthUrl
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
    await $fetch('/api/auth/totp/confirm', {
      method: 'POST',
      body: { code: totpCode.value.trim() },
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
    await $fetch('/api/auth/totp/disable', {
      method: 'POST',
      body: { currentPassword: disablePwd.value, totpCode: disableTotpCode.value },
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
      <div class="text-sm">
        {{ user?.username ?? '—' }}
      </div>
    </UCard>

    <UCard class="mb-6">
      <template #header>
        <div class="font-medium">
          Смена пароля
        </div>
      </template>
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
        <UButton
          color="primary"
          variant="soft"
          icon="i-lucide-shield-plus"
          :loading="totpLoading"
          @click="startSetup"
        >
          Включить 2FA
        </UButton>
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
          2FA активна. Для отключения подтверди пароль.
        </p>
        <UButton
          color="error"
          variant="soft"
          icon="i-lucide-shield-off"
          @click="disableConfirm = true"
        >
          Выключить 2FA
        </UButton>
      </div>
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
        <UButton color="neutral" variant="soft" @click="disableConfirm = false">
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
