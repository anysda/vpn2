<script setup lang="ts">
const { busy, act } = useAct()
const { load: loadPublic } = useSsoPublic()

type TextKey = 'authentikUrl' | 'appSlug' | 'clientId' | 'clientSecret' | 'allowedSubs' | 'redirectUrl' | 'label'
const form = reactive({
  enabled: false, authentikUrl: '', appSlug: '', clientId: '', clientSecret: '',
  allowedSubs: '', label: 'Authentik', autoRedirect: true, passwordLogin: true, redirectUrl: '',
})
const secretMasked = ref('')
const saved = ref('')
const dirty = computed(() => JSON.stringify(form) !== saved.value)

// Форма заперта, пока настройки не пришли или перечитываются.
const { data: stored, error: loadError, status, refresh: reload } = useFetch('/api/admin/sso', { server: false })
const loaded = computed(() => status.value === 'success')
watch(stored, (v) => {
  if (!v) return
  const { clientSecretMasked, ...rest } = v
  Object.assign(form, rest, { clientSecret: '' })
  saved.value = JSON.stringify(form)
  secretMasked.value = clientSecretMasked
}, { immediate: true })

const checking = ref(false)
const check = ref<{ ok: boolean, text: string } | null>(null)

const fields = computed<{ key: TextKey, label: string, ph?: string, hint?: string, mono?: boolean, secret?: boolean }[]>(() => [
  { key: 'clientId', label: 'client_id', mono: true },
  { key: 'clientSecret', label: 'client_secret', mono: true, secret: true, ph: secretMasked.value ? 'оставить прежний' : '', hint: secretMasked.value && `сохранён: ${secretMasked.value}` },
  { key: 'allowedSubs', label: 'Кого пускать', mono: true, hint: 'uuid пользователя Authentik (sub), через запятую; с логином панели: uuid:логин' },
  { key: 'redirectUrl', label: 'Адрес возврата', mono: true, hint: 'Его же впишите в Redirect URI провайдера в Authentik' },
  { key: 'label', label: 'Подпись кнопки', ph: 'Authentik' },
])

async function runCheck() {
  checking.value = true
  const body = { authentikUrl: form.authentikUrl, appSlug: form.appSlug }
  check.value = await $fetch('/api/admin/sso/check', { method: 'POST', body }).then(
    r => ({ ok: true, text: `Discovery отвечает, issuer: ${r.issuer}` }),
    e => ({ ok: false, text: errText(e) }))
  checking.value = false
}

function save() {
  if (!loaded.value || busy.value.has('save') || !dirty.value) return
  return act('save', async () => {
    await $fetch<unknown>('/api/admin/sso', { method: 'PUT', body: form })
    await Promise.all([reload(), loadPublic()])
  }, { title: 'Настройки входа сохранены', color: 'success' }, { title: 'Не сохранено' })
}
</script>

<template>
  <form class="card sc ssso" @submit.prevent="save">
    <div class="sh">
      <h3>Вход через Authentik</h3>
      <div class="ha">
        <span v-if="form.enabled" class="tag ok"><span class="dot bg-(--green)" />Включён</span>
        <span v-else class="tag">Выключен</span>
        <PanelSwitch v-model="form.enabled" label="Вход через Authentik" :disabled="!loaded || busy.has('save')" />
      </div>
    </div>
    <template v-if="loadError">
      <div role="alert" class="al red">
        <b>Не удалось загрузить настройки входа</b>{{ errText(loadError) }}
      </div>
      <div class="fld">
        <span /><code />
        <button type="button" class="pill" :disabled="status === 'pending'" @click="reload()">
          Повторить загрузку
        </button>
      </div>
    </template>
    <fieldset :disabled="!loaded || busy.has('save')" class="fg2">
      <label class="fld"><span>Адрес Authentik или discovery URL</span><input v-model="form.authentikUrl" class="in mono" placeholder="https://auth.example.com"></label>
      <label class="fld"><span>Slug приложения</span><input v-model="form.appSlug" class="in mono" placeholder="vpn2"><small class="hl">не нужен, если выше discovery URL</small></label>
      <div class="fld">
        <span>Проверка</span>
        <code class="sub2" role="status">
          <template v-if="checking">проверяю…</template>
          <span v-else-if="check" :class="check.ok ? 'text-(--green)' : 'text-(--red)'">{{ check.text }}</span>
          <template v-else>ещё не проверяли</template>
        </code>
        <button type="button" class="pill" :disabled="checking || !form.authentikUrl" @click="runCheck">
          Проверить
        </button>
      </div>
      <label v-for="f in fields" :key="f.key" class="fld">
        <span>{{ f.label }}</span>
        <input v-model="form[f.key]" class="in" :class="{ mono: f.mono }" :type="f.secret ? 'password' : 'text'" :autocomplete="f.secret ? 'new-password' : 'off'" :placeholder="f.ph">
        <small v-if="f.hint" class="hl">{{ f.hint }}</small>
      </label>
      <div class="fld">
        <span>Бесшовный вход</span><code>сразу уводить в Authentik</code>
        <PanelSwitch v-model="form.autoRedirect" label="Бесшовный вход" />
      </div>
      <div class="fld">
        <span>Оставить вход по паролю</span>
        <code v-if="form.passwordLogin">аварийный вход /login?direct=1 работает</code>
        <code v-else class="text-(--amber)">Без пароля аварийного входа /login?direct=1 нет. Выключается отдельным сохранением, после входа через Authentik.</code>
        <PanelSwitch v-model="form.passwordLogin" label="Оставить вход по паролю" />
      </div>
      <div class="fld">
        <span /><code />
        <button type="submit" class="pill w" :disabled="!dirty">Сохранить</button>
      </div>
    </fieldset>
  </form>
</template>
