<script setup lang="ts">
import QRCode from 'qrcode-svg'

useHead({ title: 'Настройки — anysda-vpn2' })
definePageMeta({ title: 'Настройки', sub: 'Бот, профиль и вход в панель' })

const { user, loginMethod, fetch: refreshSession } = useAuthSession()
const toast = useToast()
const { sso, load: loadSso } = useSsoPublic()

const viaSso = computed(() => loginMethod.value === 'sso')

// ---------------------- Password change ----------------------
const pwd = reactive({ current: '', next: '', confirm: '' })
const pwdLoading = ref(false)

async function changePassword() {
  if (!pwd.current) {
    toast.add({ title: 'Введите текущий пароль', color: 'error' })
    return
  }
  if (pwd.next.length < 8) {
    toast.add({ title: 'Пароль должен быть ≥ 8 символов', color: 'error' })
    return
  }
  if (pwd.next !== pwd.confirm) {
    toast.add({ title: 'Пароли не совпадают', color: 'error' })
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
  return new QRCode({ content: totpUri.value, padding: 4, ecl: 'M', swap: true, container: 'svg-viewbox' }).svg()
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
  <main class="tab on stg">
    <div class="head">
      <div>
        <h1>Настройки</h1>
        <p>Бот, профиль и вход в панель</p>
      </div>
    </div>
    <section class="sgrid">
      <div class="card sc">
        <div class="sh">
          <h3>Мой профиль</h3>
          <span v-if="viaSso" class="tag">вход через {{ sso.label }}</span>
        </div>
        <div class="fld">
          <span>Логин</span><code>{{ user?.username ?? '—' }}</code>
        </div>
        <div v-if="sso.enabled" class="al">
          <b>Пароль — аварийный вход</b>Форма входа по паролю открывается по адресу /login?direct=1, если включено «Оставить вход по паролю». При выключенной настройке аварийный вход тоже недоступен.
        </div>
        <form class="pwf" @submit.prevent="changePassword">
          <label class="fld"><span>Текущий пароль</span><input v-model="pwd.current" class="in" type="password" autocomplete="current-password"></label>
          <label class="fld"><span>Новый пароль</span><input v-model="pwd.next" class="in" type="password" placeholder="не короче 8 символов" autocomplete="new-password"></label>
          <label class="fld"><span>Повторите</span><input v-model="pwd.confirm" class="in" type="password" autocomplete="new-password"></label>
          <div class="fld">
            <span /><code class="sub2" />
            <button type="submit" class="pill w" :disabled="pwdLoading">Сохранить пароль</button>
          </div>
        </form>

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
              <div class="qr" v-html="totpQrSvg" />
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
      </div>

      <BotsSection />

      <form class="card sc ssso" @submit.prevent="saveSso">
        <div class="sh">
          <h3>Вход через Authentik</h3>
          <div class="ha">
            <span v-if="ssoForm.enabled" class="tag ok"><span class="dot" style="background:var(--green)" />Включён</span>
            <span v-else class="tag">Выключен</span>
            <button
              type="button"
              role="switch"
              class="tog"
              :class="{ on: ssoForm.enabled }"
              :aria-checked="ssoForm.enabled"
              aria-label="Вход через Authentik"
              :disabled="!ssoLoaded || ssoSaving"
              @click="ssoForm.enabled = !ssoForm.enabled"
            ><i /></button>
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
            v-model="ssoForm.clientSecret"
            class="in mono"
            type="password"
            autocomplete="new-password"
            :placeholder="ssoSecretMasked ? 'оставить прежний' : ''"
          ><small v-if="ssoSecretMasked" class="hl">сохранён: {{ ssoSecretMasked }}</small></label>
          <label class="fld"><span>Кого пускать</span><input v-model="ssoForm.allowedSubs" class="in mono"><small class="hl">uuid пользователя Authentik (sub), через запятую; с логином панели: uuid:логин</small></label>
          <label class="fld"><span>Адрес возврата</span><input v-model="ssoForm.redirectUrl" class="in mono"><small class="hl">Его же впишите в Redirect URI провайдера в Authentik</small></label>
          <label class="fld"><span>Подпись кнопки</span><input v-model="ssoForm.label" class="in" placeholder="Authentik"></label>
          <div class="fld">
            <span>Бесшовный вход</span><code>сразу уводить в Authentik</code>
            <button
              type="button"
              role="switch"
              class="tog"
              :class="{ on: ssoForm.autoRedirect }"
              :aria-checked="ssoForm.autoRedirect"
              aria-label="Бесшовный вход"
              @click="ssoForm.autoRedirect = !ssoForm.autoRedirect"
            ><i /></button>
          </div>
          <div class="fld">
            <span>Оставить вход по паролю</span>
            <code v-if="ssoForm.passwordLogin">аварийный вход /login?direct=1 работает</code>
            <code v-else style="color:var(--amber)">Без пароля аварийного входа /login?direct=1 нет. Выключается отдельным сохранением, после входа через Authentik.</code>
            <button
              type="button"
              role="switch"
              class="tog"
              :class="{ on: ssoForm.passwordLogin }"
              :aria-checked="ssoForm.passwordLogin"
              aria-label="Оставить вход по паролю"
              @click="ssoForm.passwordLogin = !ssoForm.passwordLogin"
            ><i /></button>
          </div>
          <div class="fld">
            <span /><code />
            <button type="submit" class="pill w" :disabled="!ssoDirty">Сохранить</button>
          </div>
        </fieldset>
      </form>
    </section>

    <UModal
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
  </main>
</template>

<style>
/* Настройки: классы макета #t-settings, под корнем .stg */
.stg .sgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.stg .card h3{font-size:14px;font-weight:500;color:var(--t2)}
.stg .sc{padding:20px 22px;display:flex;flex-direction:column;min-height:270px}
.stg .sh{display:flex;justify-content:space-between;align-items:center}
.stg .sh h3{color:var(--t1);font-size:15px}
.stg .ha{display:flex;gap:8px;align-items:center}
.stg .tag{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:999px;background:rgba(var(--fg),.06);color:var(--t2);font-size:11.5px;white-space:nowrap}
.stg .tag.ok{color:var(--green);border:1px solid rgba(var(--grn-rgb),.3);background:rgba(var(--grn-rgb),.06)}
.stg .tag.badt{color:var(--red);border:1px solid rgba(var(--red-rgb),.35);background:rgba(var(--red-rgb),.08)}
.stg .sbig{margin:18px 0 14px}
.stg .sbig .big{font-size:34px}
.stg .ys{display:grid;grid-template-columns:repeat(3,1fr);margin:0 0 6px}
.stg .ys div{display:flex;flex-direction:column;gap:4px;padding-left:14px;border-left:1px solid rgba(var(--fg),.07)}
.stg .ys div:first-child{padding-left:0;border-left:0}
.stg .ys span{font-size:11.5px;color:var(--t3)}
.stg .ys b{font-weight:400;font-size:17px}
.stg .fld{display:flex;align-items:center;gap:12px;padding:12px 0;border-top:1px solid rgba(var(--fg),.05);margin-top:auto}
.stg .fld+.fld{margin-top:0}
.stg .sh+.fld{margin-top:14px}
.stg .fld>span:first-child{color:var(--t3);font-size:12.5px;width:170px;flex:none}
.stg .fld code{font:400 13px Manrope,system-ui,sans-serif;color:var(--t1);flex:1}
.stg .fld .pill{height:30px;font-size:12px}
.stg .fld .in{flex:1;height:36px}
.stg .fld .hl{flex:none;max-width:210px;font-size:11.5px;color:var(--t3);line-height:1.35}
.stg .fld .xb{flex:none}
.stg .sub2{color:var(--t3);font-size:12.5px;margin:0}
.stg .fld code.sub2{color:var(--t3);font-size:12.5px}
.stg .al b{color:var(--t1);font-weight:500}
.stg .al.red b{color:var(--red-t)}
.stg .sc .al+.fld,.stg .sc .al+.pwf>.fld:first-child{margin-top:10px}
.stg .pwf{display:contents}
.stg fieldset.fg2{border:0;margin:0;padding:0;min-width:0}
.stg .ssso{grid-column:2;grid-row:1/3}
.stg .tog{width:42px;height:24px;border-radius:999px;border:0;padding:0;background:var(--tog);position:relative;cursor:pointer;flex:none;transition:background .2s}
.stg .tog i{position:absolute;left:3px;top:3px;width:18px;height:18px;border-radius:50%;background:var(--tog-k);transition:transform .25s var(--ease),background-color .2s}
.stg .tog.on{background:var(--acc);box-shadow:0 0 14px rgba(var(--glow),calc(.3*var(--gk)))}
.stg .tog.on i{transform:translateX(18px);background:var(--acc-t)}
.stg .tog:active i{transform:scaleX(1.25);transform-origin:left}
.stg .tog.on:active i{transform:translateX(18px) scaleX(1.25);transform-origin:right}
.stg .tog:disabled{cursor:default}
.stg .xb,.stg-modal .xb{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;border:1px solid var(--line);background:none;color:var(--t3);cursor:pointer;transition:transform .16s var(--ease),color .2s,background-color .2s}
.stg .xb:hover,.stg-modal .xb:hover{color:var(--t1)}
.stg .xb:active,.stg-modal .xb:active{transform:scale(.9)}
.stg .xb svg,.stg-modal .xb svg{width:14px;height:14px}
.stg .pill.red{color:var(--red-t);border-color:rgba(var(--red-rgb),.3)}
.stg .mt,.stg-modal .mt{color:var(--t2);font-size:13px;line-height:1.55;margin-top:8px}
.stg #tfa{margin-top:22px}
.stg #tfa .sh{padding-top:16px;border-top:1px solid rgba(var(--fg),.08)}
.stg #tfa .mt{margin-top:14px}
.stg #tfa form{display:contents}
.stg .qrrow{display:flex;gap:18px;align-items:flex-end}
.stg .qrrow .in{margin-bottom:4px}
.stg #tfa .qr{flex:none;display:block;width:180px;height:180px;margin:14px 0 0;padding:12px;border-radius:18px;background:#fff;box-shadow:0 0 40px rgba(var(--glow),calc(.12*var(--gk)))}
.stg #tfa .qr svg{display:block;width:100%;height:100%}
.stg #tfa .fld.qrcode{margin-top:14px}
/* модалка «Выключить 2FA?» - .ovl/.modal макета поверх UModal */
.stg-ovl{background:var(--ovl);backdrop-filter:blur(6px)}
.stg-modal.card{width:min(460px,calc(100vw - 48px));max-height:calc(100vh - 48px);overflow:auto;padding:22px 24px;background:linear-gradient(180deg,var(--pop-a),var(--pop-b));scrollbar-width:thin;scrollbar-color:rgba(var(--fg),.14) transparent}
.stg-modal[data-state=open]{animation:stg-m-in .26s var(--ease)}
.stg-modal[data-state=open] form>*{animation:stg-m-row .3s var(--ease) backwards}
.stg-modal[data-state=open] form>:nth-child(2){animation-delay:75ms}
.stg-modal[data-state=open] form>:nth-child(3){animation-delay:110ms}
.stg-modal[data-state=open] form>:nth-child(4){animation-delay:145ms}
.stg-modal form>:first-child{animation-delay:40ms}
.stg-modal .mh{display:flex;align-items:center;justify-content:space-between;gap:12px}
.stg-modal .mh h2{font-size:18px;font-weight:500;letter-spacing:-.01em}
.stg-modal .pill.redw{background:var(--red);border-color:var(--red);color:#fff}
@keyframes stg-m-in{from{transform:translateY(8px) scale(.97)}}
@keyframes stg-m-row{from{opacity:0;transform:translateY(6px)}}
@media (prefers-reduced-motion:reduce){.stg-modal[data-state=open],.stg-modal[data-state=open] form>*{animation:none}}
</style>
