<script setup lang="ts">
/** Карточка «Мой профиль»: логин, смена пароля и двухфакторный вход. */
const { user, loginMethod } = useAuthSession()
const toast = useToast()
const { sso } = useSsoPublic()

const viaSso = computed(() => loginMethod.value === 'sso')

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
</script>

<template>
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

    <SettingsTwoFactor />
  </div>
</template>
