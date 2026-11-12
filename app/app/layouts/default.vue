<script setup lang="ts">
const { user, clear } = useAuthSession()
const route = useRoute()
const colorMode = useColorMode()
const pageSub = usePageSub()
const period = useState<'day' | 'week'>('ov-period', () => 'day')
const name = computed(() => user.value?.username ?? 'admin')
const loggingOut = ref(false)

async function logout() {
  loggingOut.value = true
  // Снять опросы страницы до удаления сессии, иначе запрос догоняет sign-out ложным 401.
  await nextTick()
  await $fetch<unknown>('/api/auth/sign-out', { method: 'POST', body: {} }).catch(() => {})
  clear()
  // ?direct=1: сессия в Authentik живёт дальше, авторедирект внёс бы обратно тем же кликом.
  await navigateTo('/login?direct=1')
}

const THEMES = [['system', 'Как в системе'], ['light', 'Светлая'], ['dark', 'Тёмная']] as const
const menu = computed(() => [
  [{ label: 'Мой профиль', to: '/settings' }],
  THEMES.map(([value, label]) => ({
    label: `Тема: ${label.toLowerCase()}`,
    type: 'checkbox' as const,
    checked: colorMode.preference === value,
    onSelect: () => { colorMode.preference = value },
  })),
  [{ label: 'Выйти', color: 'error' as const, onSelect: logout }],
])
</script>

<template>
  <div v-if="!loggingOut">
    <div class="light" />
    <div class="wrap">
      <header>
        <NuxtLink v-if="route.path === '/clients' && route.query.id" to="/clients" class="ib cbk" aria-label="Назад">
          <svg class="i" viewBox="0 0 24 24"><path d="m15 18-6-6 6-6" /></svg>
        </NuxtLink>
        <div class="logo">
          <span>anysda-vpn2</span>
        </div>
        <div class="mhead">
          <h1>{{ route.meta.title }}</h1>
          <p v-if="pageSub || route.meta.sub">
            {{ pageSub || route.meta.sub }}
          </p>
        </div>
        <button v-if="route.path === '/'" type="button" class="pill mper" @click="period = period === 'day' ? 'week' : 'day'">
          {{ period === 'day' ? 'Сутки' : 'Неделя' }}
        </button>
        <nav class="tabs">
          <NuxtLink v-for="t in TAB_PAGES" :key="t.path" :to="t.path" :class="{ on: route.path === t.path }">
            {{ t.label }}
          </NuxtLink>
        </nav>
        <div class="hr">
          <BotBell />
          <UDropdownMenu :items="menu" :content="{ align: 'end' }">
            <button type="button" class="me" :aria-label="`Меню @${name}`">
              <i aria-hidden="true">{{ name.charAt(0).toUpperCase() }}</i>
              <div>@{{ name }}</div>
            </button>
          </UDropdownMenu>
        </div>
      </header>
      <slot />
      <div class="mbar">
        <nav class="tabs" aria-label="Разделы">
          <NuxtLink v-for="t in TAB_PAGES" :key="t.path" :to="t.path" :class="{ on: route.path === t.path }">
            {{ t.label }}
          </NuxtLink>
        </nav>
      </div>
    </div>
    <AskDialog />
  </div>
  <p v-else role="status" class="p-6">
    Выход…
  </p>
</template>
