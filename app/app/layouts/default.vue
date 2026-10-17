<script setup lang="ts">
import { TAB_PAGES } from '~/utils/tab-pages'

const { user, clear } = useAuthSession()
const route = useRoute()
const loggingOut = ref(false)
const pop = useHeaderPop()
// Телефон: заголовок раздела и период Обзора — в шапке, вкладки — в таб-баре снизу.
const mobile = useMobile()
const pageSub = usePageSub()
const head = computed(() => ({
  title: route.meta.title ?? '',
  sub: pageSub.value.path === route.path ? pageSub.value.text : route.meta.sub,
}))
const period = useOverviewPeriod()
const stack = useBackStack()
// «Назад» экрана поверх раздела (карточка клиента) — первым в шапке, как на экране.
const headerBack = useHeaderBack()
const backTo = computed(() => (mobile.value && headerBack.value.path === route.path ? headerBack.value.to : ''))

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
    // Сначала шаг назад закрытой шторки меню, иначе он вернёт со входа обратно.
    await stack.idle()
    // ?direct=1, а не просто /login: сессия в самом Authentik живёт дальше, и
    // без этого авторедирект внёс бы обратно тем же кликом — «выйти» выглядело
    // бы как «ничего не произошло».
    await navigateTo('/login?direct=1')
  }
}

const name = computed(() => user.value?.username ?? 'admin')
const initial = computed(() => name.value.charAt(0).toUpperCase())

// Пункты меню профиля одни на ПК и в шторке телефона.
const PROFILE_LINKS = [
  { to: '/settings', label: 'Мой профиль' },
  { to: '/screen', label: 'Настроить экран' },
]

function toggleMenu() {
  pop.value = pop.value === 'me' ? null : 'me'
}

// Скользящая пилюля под активной вкладкой, как в макете: в шапке и в таб-баре.
const nav = ref<HTMLElement>()
const glideEl = ref<HTMLElement>()
const glided = ref(false)
const bar = ref<HTMLElement>()
const barGlideEl = ref<HTMLElement>()
const barGlided = ref(false)
function place(box: HTMLElement | undefined, g: HTMLElement | undefined, done: Ref<boolean>, jump: boolean) {
  const on = box?.querySelector<HTMLElement>(':scope>a.on')
  if (!box || !g || !on || !box.offsetWidth) {
    done.value = false
    return
  }
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches
  g.classList.toggle('now', jump || !done.value || reduce)
  const r = on.getBoundingClientRect(), q = box.getBoundingClientRect()
  g.style.height = `${r.height}px`
  g.style.width = `${r.width}px`
  g.style.transform = `translate(${r.left - q.left - box.clientLeft}px,${r.top - q.top - box.clientTop}px)`
  if (!done.value || jump) requestAnimationFrame(() => g.classList.remove('now'))
  done.value = true
}
function glide(jump = false) {
  place(nav.value, glideEl.value, glided, jump)
  place(bar.value, barGlideEl.value, barGlided, jump)
}
const onResize = () => glide(true)

// Телефон: шапка фиксирована, и отступ контента идёт за её настоящей высотой
// (подзаголовок, «Назад», вырез экрана), а не за константой.
const headEl = ref<HTMLElement>()
const headH = ref(0)
let headRo: ResizeObserver | undefined
watch(() => route.path, () => nextTick(() => requestAnimationFrame(() => glide())))

function onDocClick(e: MouseEvent) {
  if (!(e.target as Element).closest?.('.pop, .bsh')) pop.value = null
  else if ((e.target as Element).closest('a')) pop.value = null
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') pop.value = null
}
onMounted(() => {
  glide(true)
  headRo = new ResizeObserver(([e]) => { headH.value = e?.borderBoxSize[0]?.blockSize ?? 0 })
  if (headEl.value) headRo.observe(headEl.value, { box: 'border-box' })
  void document.fonts?.ready.then(() => glide(true))
  addEventListener('resize', onResize)
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKey)
})
onUnmounted(() => {
  headRo?.disconnect()
  removeEventListener('resize', onResize)
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div v-if="!loggingOut">
    <div class="light" />
    <div class="wrap" :style="headH ? { '--mh': `${headH}px` } : undefined">
      <header ref="headEl">
        <button v-if="backTo" type="button" class="ib cbk" aria-label="Назад" @click="stack.leave(backTo)">
          <svg class="i" viewBox="0 0 24 24"><path d="m15 18-6-6 6-6" /></svg>
        </button>
        <div class="logo">
          <span>anysda-vpn2</span>
        </div>
        <div v-if="mobile" class="mhead">
          <h1>{{ head.title }}</h1>
          <p v-if="head.sub">
            {{ head.sub }}
          </p>
        </div>
        <button v-if="mobile && route.path === '/'" type="button" class="pill mper" @click="period = period === 'day' ? 'week' : 'day'">
          <svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M7 12h10M10 18h4" /></svg>{{ period === 'day' ? 'Сутки' : 'Неделя' }}
        </button>
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
              :aria-label="mobile ? `Меню @${name}` : undefined"
              @click.stop="toggleMenu"
            >
              <i aria-hidden="true">{{ initial }}</i>
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
              <NuxtLink v-for="l in PROFILE_LINKS" :key="l.to" :to="l.to" role="menuitem">
                {{ l.label }}
              </NuxtLink>
              <ThemeSwitch />
              <hr>
              <a class="out" role="menuitem" tabindex="0" @click="logout" @keydown.enter="logout">Выйти</a>
            </div>
          </div>
        </div>
      </header>
      <slot />
      <div class="mbar">
        <nav ref="bar" class="tabs" :class="{ gl: barGlided }" aria-label="Разделы">
          <NuxtLink
            v-for="t in TAB_PAGES"
            :key="t.path"
            :to="t.path"
            :class="{ on: route.path === t.path }"
            :aria-current="route.path === t.path ? 'page' : undefined"
          >
            {{ t.label }}
          </NuxtLink>
          <span ref="barGlideEl" class="glide now" aria-hidden="true" />
        </nav>
      </div>
      <BottomSheet v-if="mobile" :open="pop === 'me'" label="Меню профиля" @close="pop = null">
        <div class="bsu">
          <i aria-hidden="true">{{ initial }}</i>@{{ name }}
        </div>
        <div class="menu" role="menu">
          <NuxtLink v-for="l in PROFILE_LINKS" :key="l.to" :to="l.to" role="menuitem">
            {{ l.label }}
          </NuxtLink>
          <ThemeSwitch />
          <hr>
          <a class="out" role="menuitem" tabindex="0" @click="logout" @keydown.enter="logout">Выйти</a>
        </div>
      </BottomSheet>
    </div>
  </div>
  <p v-else role="status" class="p-6">
    Выход…
  </p>
</template>
