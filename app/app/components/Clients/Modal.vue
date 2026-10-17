<script setup lang="ts">
import { anim } from '~/utils/clients-view'

// Модалка макета (.ovl/.modal): Nuxt UI не даёт такого вида и входа.
// На телефоне то же содержимое уходит в нижнюю шторку.
const props = defineProps<{ title: string, wide?: boolean, closable?: boolean }>()
const open = defineModel<boolean>('open', { default: false })
const mobile = useMobile()

const id = useId()
const mounted = ref(false)
const on = ref(false)
const box = ref<HTMLElement>()
let hideTimer: ReturnType<typeof setTimeout> | undefined
let focusTimer: ReturnType<typeof setTimeout> | undefined
let returnFocus: HTMLElement | null = null

function close() {
  open.value = false
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') close()
}

/** Пользователь уже стоит в поле окна: автофокус его не перебивает. */
function typing() {
  const a = document.activeElement as HTMLElement | null
  return !!a && !!box.value?.contains(a) && (a.matches('input,textarea,select') || a.isContentEditable)
}

watch(open, (now) => {
  clearTimeout(hideTimer)
  if (now) {
    returnFocus = document.activeElement as HTMLElement | null
    mounted.value = true
    // Шторка ловит Escape и фокус сама.
    if (!mobile.value) document.addEventListener('keydown', onKey)
    nextTick(() => requestAnimationFrame(() => {
      if (!open.value) return
      on.value = true
      if (mobile.value) return
      ;[...(box.value?.children ?? [])].slice(0, 6).forEach((c, i) =>
        anim(c, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], { duration: 300, delay: 40 + i * 35 }))
      focusTimer = setTimeout(() => {
        if (typing()) return
        const first = box.value?.querySelector<HTMLElement>('input:not([readonly])')
        ;(first ?? box.value)?.focus()
      }, 60)
    }))
  }
  else if (mounted.value) {
    on.value = false
    clearTimeout(focusTimer)
    document.removeEventListener('keydown', onKey)
    // Шторка уезжает .32s, окно гаснет .16s.
    hideTimer = setTimeout(() => { mounted.value = false }, mobile.value ? 340 : 200)
    if (returnFocus?.isConnected) returnFocus.focus()
    returnFocus = null
  }
}, { immediate: true })

onUnmounted(() => {
  clearTimeout(hideTimer)
  clearTimeout(focusTimer)
  document.removeEventListener('keydown', onKey)
})

const titleId = computed(() => `${id}-t`)
</script>

<template>
  <BottomSheet v-if="mobile && mounted" :open="on" :label="props.title" @close="close">
    <div ref="box" class="clm bs" :class="{ wide }">
      <div class="mh">
        <h2>{{ title }}</h2>
        <button
          v-if="closable"
          type="button"
          class="xb"
          aria-label="Закрыть"
          @click="close"
        >
          <svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>
      <slot />
    </div>
  </BottomSheet>
  <Teleport v-else-if="mounted" to="body">
    <div
      class="ovl clm"
      :class="{ on }"
      @mousedown.self="close"
    >
      <div
        ref="box"
        class="card modal"
        :class="{ wide }"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        tabindex="-1"
      >
        <div class="mh">
          <h2 :id="titleId">
            {{ title }}
          </h2>
          <button
            v-if="closable"
            type="button"
            class="xb"
            aria-label="Закрыть"
            @click="close"
          >
            <svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
          </button>
        </div>
        <slot />
      </div>
    </div>
  </Teleport>
</template>
