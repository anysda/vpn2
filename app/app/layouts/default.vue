<script setup lang="ts">
import { TAB_PAGES } from '~/utils/tab-pages'

const { user, clear } = useAuthSession()
const route = useRoute()
const loggingOut = ref(false)
const pop = useHeaderPop()

async function logout() {
  if (loggingOut.value) return
  pop.value = null
  loggingOut.value = true
  // Снять интервалы и отменить useFetch до удаления сессии: иначе запрос,
  // начавшийся на главной, догоняет sign-out и получает ложный 401.
  await nextTick()
  try {
    await $fetch('/api/auth/sign-out', { method: 'POST', body: {} })
  }
  finally {
    clear()
    // ?direct=1, а не просто /login: сессия в самом Authentik живёт дальше, и
    // без этого авторедирект внёс бы обратно тем же кликом — «выйти» выглядело
    // бы как «ничего не произошло».
    await navigateTo('/login?direct=1')
  }
}

const name = computed(() => user.value?.username ?? 'admin')

function toggleMenu() {
  pop.value = pop.value === 'me' ? null : 'me'
}

// Скользящая пилюля под активной вкладкой, как в макете.
const nav = ref<HTMLElement>()
const glideEl = ref<HTMLElement>()
const glided = ref(false)
function glide(jump = false) {
  const box = nav.value, g = glideEl.value
  const on = box?.querySelector<HTMLElement>(':scope>a.on')
  if (!box || !g || !on || !box.offsetWidth) {
    glided.value = false
    return
  }
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches
  g.classList.toggle('now', jump || !glided.value || reduce)
  const r = on.getBoundingClientRect(), q = box.getBoundingClientRect()
  g.style.height = `${r.height}px`
  g.style.width = `${r.width}px`
  g.style.transform = `translate(${r.left - q.left - box.clientLeft}px,${r.top - q.top - box.clientTop}px)`
  if (!glided.value || jump) requestAnimationFrame(() => g.classList.remove('now'))
  glided.value = true
}
const onResize = () => glide(true)
watch(() => route.path, () => nextTick(() => requestAnimationFrame(() => glide())))

function onDocClick(e: MouseEvent) {
  if (!(e.target as Element).closest?.('.pop')) pop.value = null
  else if ((e.target as Element).closest('a')) pop.value = null
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') pop.value = null
}
onMounted(() => {
  glide(true)
  void document.fonts?.ready.then(() => glide(true))
  addEventListener('resize', onResize)
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKey)
})
onUnmounted(() => {
  removeEventListener('resize', onResize)
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div v-if="!loggingOut">
    <div class="light" />
    <div class="wrap">
      <header>
        <div class="logo">
          <span>anysda-vpn2</span>
        </div>
        <nav ref="nav" class="tabs" :class="{ gl: glided }">
          <NuxtLink
            v-for="t in TAB_PAGES"
            :key="t.path"
            :to="t.path"
            :class="{ on: route.path === t.path }"
            :aria-current="route.path === t.path ? 'page' : undefined"
          >
            {{ t.label }}
          </NuxtLink>
          <span ref="glideEl" class="glide now" aria-hidden="true" />
        </nav>
        <div class="hr">
          <BotDeliveryStatus />
          <div class="pw">
            <button
              type="button"
              class="me"
              :aria-expanded="pop === 'me'"
              aria-haspopup="menu"
              @click.stop="toggleMenu"
            >
              <i aria-hidden="true">{{ name.charAt(0).toUpperCase() }}</i>
              <div>@{{ name }}</div>
              <svg class="i cv" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10l5 5 5-5" /></svg>
            </button>
            <div
              class="card pop menu"
              :class="{ on: pop === 'me' }"
              role="menu"
              :aria-hidden="pop !== 'me'"
              :inert="pop !== 'me'"
            >
              <NuxtLink to="/settings" role="menuitem">
                Мой профиль
              </NuxtLink>
              <NuxtLink to="/screen" role="menuitem">
                Настроить экран
              </NuxtLink>
              <ThemeSwitch />
              <hr>
              <a class="out" role="menuitem" tabindex="0" @click="logout" @keydown.enter="logout">Выйти</a>
            </div>
          </div>
        </div>
      </header>
      <slot />
    </div>
  </div>
  <p v-else role="status" class="p-6">
    Выход…
  </p>
</template>
