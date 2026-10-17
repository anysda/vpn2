<script setup lang="ts">
definePageMeta({ layout: false })

const { loggedIn, fetch: refreshSession } = useAuthSession()
const route = useRoute()
// Свежие настройки SSO: их могли поменять в профиле перед выходом.
const { sso, load: loadSso } = useSsoPublic()
await loadSso()

if (loggedIn.value) {
  await navigateTo('/')
}

// Break-glass: /login?direct=1 показывает форму даже при включённом SSO. Сюда же
// приводит любая ошибка входа через IdP — иначе авторедирект зациклил бы её.
const ssoError = computed(() => (route.query.error as string | undefined) ?? null)
const showForm = computed(() => !sso.value.enabled || !sso.value.autoRedirect || route.query.direct !== undefined || !!ssoError.value)

if (sso.value.enabled && !showForm.value) {
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

// Вход в два шага, как его ведёт better-auth: логин и пароль, затем, если у
// учётки включена 2FA, код — уже без пароля, по cookie первого шага.
async function submit() {
  error.value = null
  loading.value = true
  try {
    if (needsTotp.value) {
      await $fetch('/api/auth/two-factor/verify-totp', {
        method: 'POST',
        body: { code: state.totpCode.replace(/\s/g, '') },
      })
    }
    else {
      const res = await $fetch<{ twoFactorRedirect?: boolean }>('/api/auth/sign-in/username', {
        method: 'POST',
        body: { username: state.username, password: state.password },
      })
      if (res?.twoFactorRedirect) {
        needsTotp.value = true
        return
      }
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

// Движение входа из макета: карточка выплывает, шаг кода выезжает, ошибка
// коротко встряхивает форму.
const EASE = 'cubic-bezier(.23,1,.32,1)'
const card = ref<HTMLElement>()
const codeField = ref<HTMLElement>()
const codeInput = ref<HTMLInputElement>()
const userInput = ref<HTMLInputElement>()
const still = () => matchMedia('(prefers-reduced-motion: reduce)').matches
onMounted(() => {
  userInput.value?.focus()
  if (still()) return
  card.value?.animate([{ opacity: 0, transform: 'translateY(10px) scale(.98)' }, { opacity: 1, transform: 'none' }],
    { duration: 500, easing: EASE, fill: 'backwards' })
})
watch(needsTotp, async (on) => {
  if (!on) return
  await nextTick()
  if (!still()) {
    codeField.value?.animate([{ opacity: 0, transform: 'translateY(-6px)' }, { opacity: 1, transform: 'none' }],
      { duration: 300, easing: EASE, fill: 'backwards' })
  }
  setTimeout(() => codeInput.value?.focus(), 30)
})
watch(error, (msg) => {
  if (!msg || still()) return
  card.value?.animate([{ transform: 'translateX(0)' }, { transform: 'translateX(-7px)' }, { transform: 'translateX(6px)' }, { transform: 'translateX(-3px)' }, { transform: 'translateX(0)' }],
    { duration: 340, easing: 'ease-out' })
})
</script>

<template>
  <div>
    <div class="light" />
    <div class="wrap lgw">
      <main class="tab on bare login">
        <div ref="card" class="card lgn">
          <div class="logo" style="justify-content:center">
            <span>anysda-vpn2</span>
          </div>
          <div v-if="ssoErrorText" class="al red" role="alert">
            {{ ssoErrorText }}
          </div>

          <!-- Кнопка нужна и при autoRedirect: на эту страницу попадают только
               через break-glass (?direct=1) или после ошибки — обратный путь в SSO
               должен оставаться в один клик. -->
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
                ref="userInput"
                v-model="state.username"
                class="in"
                required
                :disabled="needsTotp || loading"
                autocomplete="username"
                autocapitalize="none"
                spellcheck="false"
              >
            </label>
            <label class="fl2">
              <span>Пароль</span>
              <input
                v-model="state.password"
                class="in"
                type="password"
                required
                :disabled="needsTotp || loading"
                autocomplete="current-password"
              >
            </label>
            <label v-if="needsTotp" ref="codeField" class="fl2">
              <span>Код из аутентификатора</span>
              <input
                ref="codeInput"
                v-model="state.totpCode"
                class="in mono"
                required
                placeholder="123 456"
                inputmode="numeric"
                enterkeyhint="done"
                autocomplete="one-time-code"
              >
            </label>
            <div v-if="error" class="al red" role="alert">
              {{ error }}
            </div>
            <div class="mf">
              <button
                v-if="needsTotp"
                type="button"
                class="pill"
                :disabled="loading"
                @click="reset"
              >
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


<style>
/* Вход на телефоне: карточка по центру экрана во всю ширину, поля и кнопки
   под палец (46px). Шире 720px остаются правила .lgn из main.css. */
@media (max-width:720px){
  .wrap.lgw{display:flex;flex-direction:column;min-height:100dvh;padding:calc(16px + env(safe-area-inset-top)) 16px calc(16px + env(safe-area-inset-bottom))}
  .wrap.lgw>.login{flex:1;display:flex;flex-direction:column;justify-content:center;align-items:stretch;min-height:0}
  .lgw .lgn{width:auto;padding:30px 22px 24px}
  .lgw .lgn .logo span{font-size:18px}
  .lgw .lgn .lsso{height:46px;font-size:14px}
  .lgw .lgn .in{height:46px;font-size:15px}
  .lgw .lgn .in.mono{font-size:17px!important;letter-spacing:.12em}
  .lgw .lgn .mf .pill{height:46px}
  .lgw .lgn .lgo{font-size:14px}
  .lgw .lgn .pill{touch-action:manipulation;-webkit-user-select:none;user-select:none}
}
</style>
