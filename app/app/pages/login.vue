<script setup lang="ts">
definePageMeta({ layout: false })

const { loggedIn, fetch: refreshSession } = useAuthSession()
const route = useRoute()
// Свежие настройки SSO: их могли поменять в профиле перед выходом.
const { sso, load: loadSso } = useSsoPublic()
await loadSso()

if (loggedIn.value) await navigateTo('/')

// /login?direct=1 и ошибка входа через Authentik показывают форму даже при авторедиректе.
const ssoError = computed(() => (route.query.error as string | undefined) ?? null)
const showForm = computed(() => !sso.value.enabled || !sso.value.autoRedirect || route.query.direct !== undefined || !!ssoError.value)

if (sso.value.enabled && !showForm.value) await navigateTo('/auth/authentik', { external: true })

const SSO_ERRORS: Record<string, string> = {
  sso_off: 'SSO выключен или не настроен — вход по паролю',
  sso_failed: 'Authentik не завершил вход. Попробуйте ещё раз или войдите по паролю',
  sso_no_sub: 'Authentik не вернул идентификатор пользователя',
  sso_not_allowed: 'Этой учётной записи Authentik вход в панель не разрешён',
  sso_no_local_user: 'В панели нет учётки, к которой можно привязать этот вход',
  sso_ambiguous: 'В панели несколько учёток — привязку нужно задать явно',
  sso_already_linked: 'Учётка панели уже привязана к другому пользователю Authentik',
}
const ssoErrorText = computed(() => ssoError.value ? (SSO_ERRORS[ssoError.value] ?? `Ошибка SSO: ${ssoError.value}`) : null)

const state = reactive({ username: '', password: '', totpCode: '' })

const needsTotp = ref(false)
const error = ref<string | null>(null)
const loading = ref(false)

// Вход в два шага: логин и пароль, затем код 2FA по cookie первого шага; true - вход завершён.
async function signIn() {
  if (needsTotp.value) {
    await $fetch('/api/auth/two-factor/verify-totp', { method: 'POST', body: { code: state.totpCode.replace(/\s/g, '') } })
    return true
  }
  const body = { username: state.username, password: state.password }
  const res = await $fetch<{ twoFactorRedirect?: boolean }>('/api/auth/sign-in/username', { method: 'POST', body })
  needsTotp.value = !!res?.twoFactorRedirect
  return !needsTotp.value
}

async function submit() {
  error.value = null
  loading.value = true
  const done = await signIn().catch((e: unknown) => {
    error.value = errText(e)
    return false
  })
  if (done) {
    await refreshSession()
    await navigateTo('/')
  }
  loading.value = false
}

function reset() {
  needsTotp.value = false
  state.totpCode = ''
  error.value = null
}

// Курсор в поле, как только оно появилось: логин при открытии, код - на втором шаге.
const vFocus = { mounted: (el: HTMLInputElement) => el.focus() }
</script>

<template>
  <div>
    <div class="light" />
    <div class="wrap lgw">
      <main class="tab on bare login">
        <div class="card lgn">
          <div class="logo" style="justify-content:center">
            <span>anysda-vpn2</span>
          </div>
          <div v-if="ssoErrorText" class="al red" role="alert">
            {{ ssoErrorText }}
          </div>

          <template v-if="sso.enabled && !needsTotp">
            <a class="pill w lsso" href="/auth/authentik">
              <svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l8 4v5c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V7z" /></svg>Войти через {{ sso.label }}
            </a>
            <div class="or">
              <span>или по паролю</span>
            </div>
          </template>

          <form @submit.prevent="submit">
            <label class="fl2">
              <span>Логин</span>
              <input
                v-model="state.username" v-focus class="in" required :disabled="needsTotp || loading" autocomplete="username"
                autocapitalize="none" spellcheck="false">
            </label>
            <label class="fl2">
              <span>Пароль</span>
              <input v-model="state.password" class="in" type="password" required :disabled="needsTotp || loading" autocomplete="current-password">
            </label>
            <label v-if="needsTotp" class="fl2">
              <span>Код из аутентификатора</span>
              <input
                v-model="state.totpCode" v-focus class="in mono" required placeholder="123 456" inputmode="numeric" enterkeyhint="done"
                autocomplete="one-time-code">
            </label>
            <div v-if="error" class="al red" role="alert">
              {{ error }}
            </div>
            <div class="mf">
              <button v-if="needsTotp" type="button" class="pill" :disabled="loading" @click="reset">
                Назад
              </button>
              <button type="submit" class="pill w lgo" :disabled="loading">
                {{ needsTotp ? 'Подтвердить' : 'Войти' }}
              </button>
            </div>
          </form>
        </div>
      </main>
    </div>
  </div>
</template>
