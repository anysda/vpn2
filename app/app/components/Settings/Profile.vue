<script setup lang="ts">
const { user, loginMethod } = useAuthSession()
const { sso } = useSsoPublic()
const { toast, busy, act } = useAct()
const pwd = reactive({ current: '', next: '', confirm: '' })

function changePassword() {
  const bad = !pwd.current ? 'Введите текущий пароль' : pwd.next.length < 8 ? 'Пароль должен быть ≥ 8 символов' : pwd.next !== pwd.confirm ? 'Пароли не совпадают' : ''
  if (bad) return toast.add({ title: bad, color: 'error' })
  // Остальные сессии сервер рвёт, эта остаётся.
  return act('pwd', async () => {
    await $fetch<unknown>('/api/auth/change-password', { method: 'POST', body: { currentPassword: pwd.current, newPassword: pwd.next } })
    pwd.current = pwd.next = pwd.confirm = ''
  }, { title: 'Пароль обновлён', color: 'success' }, { title: 'Ошибка' })
}
</script>

<template>
  <div class="card sc">
    <div class="sh">
      <h3>Мой профиль</h3>
      <span v-if="loginMethod === 'sso'" class="tag">вход через {{ sso.label }}</span>
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
        <button type="submit" class="pill w" :disabled="busy.has('pwd')">Сохранить пароль</button>
      </div>
    </form>

    <SettingsTwoFactor />
  </div>
</template>
