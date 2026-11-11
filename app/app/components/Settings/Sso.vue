<script setup lang="ts">
/** Карточка «Вход через Authentik». */
const toast = useToast()
const { load: loadSso } = useSsoPublic()

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
// «Сохранить» оживает после правки, как в макете.
const ssoSaved = ref('')
const ssoDirty = computed(() => JSON.stringify(ssoForm) !== ssoSaved.value)

async function loadSsoForm() {
  const r = await $fetch<Omit<SsoForm, 'clientSecret'> & { clientSecretMasked: string }>('/api/admin/sso')
  const { clientSecretMasked, ...rest } = r
  Object.assign(ssoForm, rest, { clientSecret: '' })
  ssoSaved.value = JSON.stringify(ssoForm)
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
  if (!ssoLoaded.value || ssoSaving.value || !ssoDirty.value) return
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
  <form class="card sc ssso" @submit.prevent="saveSso">
    <div class="sh">
      <h3>Вход через Authentik</h3>
      <div class="ha">
        <span v-if="ssoForm.enabled" class="tag ok"><span class="dot" style="background:var(--green)" />Включён</span>
        <span v-else class="tag">Выключен</span>
        <button
          type="button" role="switch" class="tog" :class="{ on: ssoForm.enabled }" :aria-checked="ssoForm.enabled" aria-label="Вход через Authentik"
          :disabled="!ssoLoaded || ssoSaving" @click="ssoForm.enabled = !ssoForm.enabled"><i /></button>
      </div>
    </div>
    <div v-if="ssoLoadError" role="alert" class="al red">
      <b>Не удалось загрузить настройки входа</b>{{ ssoLoadError }}
    </div>
    <div v-if="ssoLoadError" class="fld">
      <span /><code />
      <button type="button" class="pill" :disabled="ssoLoading" @click="reloadSsoForm">Повторить загрузку</button>
    </div>
    <fieldset :disabled="!ssoLoaded || ssoSaving" class="fg2">
      <label class="fld"><span>Адрес Authentik или discovery URL</span><input v-model="ssoForm.authentikUrl" class="in mono" placeholder="https://auth.example.com"></label>
      <label class="fld"><span>Slug приложения</span><input v-model="ssoForm.appSlug" class="in mono" placeholder="vpn2"><small class="hl">не нужен, если выше discovery URL</small></label>
      <div class="fld">
        <span>Проверка</span>
        <code class="sub2" role="status">
          <template v-if="ssoChecking">проверяю…</template>
          <span v-else-if="ssoCheck" :style="{ color: ssoCheck.ok ? 'var(--green)' : 'var(--red)' }">{{ ssoCheck.text }}</span>
          <template v-else>ещё не проверяли</template>
        </code>
        <button type="button" class="pill" :disabled="ssoChecking || !ssoForm.authentikUrl" @click="checkSso">Проверить</button>
      </div>
      <label class="fld"><span>client_id</span><input v-model="ssoForm.clientId" class="in mono" autocomplete="off"></label>
      <label class="fld"><span>client_secret</span><input
        v-model="ssoForm.clientSecret" class="in mono" type="password" autocomplete="new-password"
        :placeholder="ssoSecretMasked ? 'оставить прежний' : ''"><small v-if="ssoSecretMasked" class="hl">сохранён: {{ ssoSecretMasked }}</small></label>
      <label class="fld"><span>Кого пускать</span><input v-model="ssoForm.allowedSubs" class="in mono"><small class="hl">uuid пользователя Authentik (sub), через запятую; с логином панели: uuid:логин</small></label>
      <label class="fld"><span>Адрес возврата</span><input v-model="ssoForm.redirectUrl" class="in mono"><small class="hl">Его же впишите в Redirect URI провайдера в Authentik</small></label>
      <label class="fld"><span>Подпись кнопки</span><input v-model="ssoForm.label" class="in" placeholder="Authentik"></label>
      <div class="fld">
        <span>Бесшовный вход</span><code>сразу уводить в Authentik</code>
        <button
          type="button" role="switch" class="tog" :class="{ on: ssoForm.autoRedirect }" :aria-checked="ssoForm.autoRedirect"
          aria-label="Бесшовный вход" @click="ssoForm.autoRedirect = !ssoForm.autoRedirect"><i /></button>
      </div>
      <div class="fld">
        <span>Оставить вход по паролю</span>
        <code v-if="ssoForm.passwordLogin">аварийный вход /login?direct=1 работает</code>
        <code v-else style="color:var(--amber)">Без пароля аварийного входа /login?direct=1 нет. Выключается отдельным сохранением, после входа через Authentik.</code>
        <button
          type="button" role="switch" class="tog" :class="{ on: ssoForm.passwordLogin }" :aria-checked="ssoForm.passwordLogin"
          aria-label="Оставить вход по паролю" @click="ssoForm.passwordLogin = !ssoForm.passwordLogin"><i /></button>
      </div>
      <div class="fld">
        <span /><code />
        <button type="submit" class="pill w" :disabled="!ssoDirty">Сохранить</button>
      </div>
    </fieldset>
  </form>
</template>
