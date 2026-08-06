<script setup lang="ts">
definePageMeta({ layout: false })

const { loggedIn, fetch: refreshSession } = useUserSession()
const route = useRoute()
const { sso } = useRuntimeConfig().public

if (loggedIn.value) {
  await navigateTo('/')
}

// Break-glass: /login?direct=1 показывает форму даже при включённом SSO. Сюда же
// приводит любая ошибка входа через IdP — иначе авторедирект зациклил бы её.
const ssoError = computed(() => (route.query.error as string | undefined) ?? null)
const showForm = computed(() => !sso.enabled || !sso.autoRedirect || route.query.direct !== undefined || !!ssoError.value)

if (sso.enabled && !showForm.value) {
  await navigateTo('/auth/authentik', { external: true })
}

const SSO_ERRORS: Record<string, string> = {
  sso_off: 'SSO выключен или не настроен — вход по паролю',
  sso_failed: 'Authentik не завершил вход. Попробуйте ещё раз или войдите по паролю',
  sso_no_sub: 'Authentik не вернул идентификатор пользователя',
  sso_not_allowed: 'Этой учётной записи Authentik вход в панель не разрешён',
  sso_no_local_user: 'В панели нет учётки, к которой можно привязать этот вход',
  sso_ambiguous: 'В панели несколько учёток — привязку нужно задать явно',
  sso_already_linked: 'Учётка панели уже привязана к другому пользователю Authentik',
}
const ssoErrorText = computed(() =>
  ssoError.value ? (SSO_ERRORS[ssoError.value] ?? `Ошибка SSO: ${ssoError.value}`) : null,
)

const state = reactive({
  username: '',
  password: '',
  totpCode: '',
})

const needsTotp = ref(false)
const error = ref<string | null>(null)
const loading = ref(false)

async function submit() {
  error.value = null
  loading.value = true
  try {
    const res = await $fetch<{ ok?: boolean, needsTotp?: boolean }>('/api/auth/login', {
      method: 'POST',
      body: {
        username: state.username,
        password: state.password,
        totpCode: needsTotp.value ? state.totpCode : undefined,
      },
    })
    if (res.needsTotp) {
      needsTotp.value = true
      return
    }
    await refreshSession()
    await navigateTo('/')
  }
  catch (e) {
    const err = e as { statusMessage?: string, message?: string }
    error.value = err.statusMessage ?? err.message ?? 'Ошибка'
  }
  finally {
    loading.value = false
  }
}

function reset() {
  needsTotp.value = false
  state.totpCode = ''
  error.value = null
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-(--ui-page) p-4">
    <UCard class="w-full max-w-sm">
      <template #header>
        <div class="text-center font-semibold text-lg">
          anysda-vpn2
        </div>
      </template>

      <UAlert
        v-if="ssoErrorText"
        class="mb-4"
        color="warning"
        variant="soft"
        :title="ssoErrorText"
        icon="i-lucide-shield-alert"
      />

      <!-- Кнопка нужна и при autoRedirect: на эту страницу попадают только
           через break-glass (?direct=1) или после ошибки — обратный путь в SSO
           должен оставаться в один клик. -->
      <div v-if="sso.enabled && !needsTotp" class="mb-4 space-y-3">
        <UButton
          block
          icon="i-lucide-key-round"
          :to="'/auth/authentik'"
          external
        >
          Войти через {{ sso.label }}
        </UButton>
        <div class="flex items-center gap-2 text-xs text-(--ui-text-muted)">
          <span class="h-px flex-1 bg-(--ui-border)" />
          или по паролю
          <span class="h-px flex-1 bg-(--ui-border)" />
        </div>
      </div>

      <form class="space-y-4" @submit.prevent="submit">
        <UFormField label="Логин" required>
          <UInput
            v-model="state.username"
            :disabled="needsTotp || loading"
            autocomplete="username"
            class="w-full"
          />
        </UFormField>
        <UFormField label="Пароль" required>
          <UInput
            v-model="state.password"
            type="password"
            :disabled="needsTotp || loading"
            autocomplete="current-password"
            class="w-full"
          />
        </UFormField>

        <UFormField v-if="needsTotp" label="Код из аутентификатора" required>
          <UInput
            v-model="state.totpCode"
            placeholder="123 456"
            autocomplete="one-time-code"
            inputmode="numeric"
            class="w-full"
            autofocus
          />
        </UFormField>

        <UAlert
          v-if="error"
          color="error"
          variant="soft"
          :title="error"
          icon="i-lucide-circle-alert"
        />

        <div class="flex gap-2">
          <UButton
            v-if="needsTotp"
            color="neutral"
            variant="outline"
            :disabled="loading"
            @click="reset"
          >
            Назад
          </UButton>
          <UButton
            type="submit"
            block
            :loading="loading"
          >
            {{ needsTotp ? 'Подтвердить' : 'Войти' }}
          </UButton>
        </div>
      </form>
    </UCard>
  </div>
</template>
