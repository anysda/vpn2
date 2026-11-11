<script setup lang="ts">
// Окно панели: карточка поверх затемнения, на телефоне - нижняя шторка с тем же
// содержимым. С `ok` внизу стоят «Отмена» и одно действие, иначе низ даёт слот.
// Класс раздела (clm, dns, stg) вешается на содержимое: по нему стили полей.
defineOptions({ inheritAttrs: false })
defineProps<{ title: string, wide?: boolean, closable?: boolean, ok?: string, okClass?: string, okDisabled?: boolean }>()
defineEmits<{ ok: [] }>()
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
function leave() {
  clearTimeout(hideTimer)
  clearTimeout(focusTimer)
  document.removeEventListener('keydown', onKey)
}

/** Строки окна поднимаются лесенкой; курсор - в первое поле, если человек не выбрал поле сам. */
function enter() {
  if (!open.value) return
  on.value = true
  if (mobile.value) return
  ;[...(box.value?.children ?? [])].slice(0, 6).forEach((c, i) => rise(c, { duration: 300, delay: 40 + i * 35 }))
  focusTimer = setTimeout(() => {
    const a = document.activeElement as HTMLElement | null
    if (a && box.value?.contains(a) && (a.matches('input,textarea,select') || a.isContentEditable)) return
    ;(box.value?.querySelector<HTMLElement>('input:not([readonly])') ?? box.value)?.focus()
  }, 60)
}

watch(open, (now) => {
  leave()
  if (now) {
    returnFocus = document.activeElement as HTMLElement | null
    mounted.value = true
    // Шторка ловит Escape и фокус сама.
    if (!mobile.value) document.addEventListener('keydown', onKey)
    nextTick(() => requestAnimationFrame(enter))
  }
  else if (mounted.value) {
    on.value = false
    // Шторка уезжает .32s, окно гаснет .16s.
    hideTimer = setTimeout(() => { mounted.value = false }, mobile.value ? 340 : 200)
    if (returnFocus?.isConnected) returnFocus.focus()
    returnFocus = null
  }
}, { immediate: true })
onUnmounted(leave)
</script>

<template>
  <BottomSheet v-if="mobile && mounted" :open="on" :label="title" @close="close">
    <div ref="box" class="bs" :class="[$attrs.class, { wide }]">
      <div class="mh">
        <h2>{{ title }}</h2>
        <button v-if="closable" type="button" class="xb" aria-label="Закрыть" @click="close">
          <svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>
      <slot />
      <div v-if="ok" class="mf">
        <button type="button" class="pill" @click="close">
          Отмена
        </button>
        <button type="button" class="pill" :class="okClass ?? 'w'" :disabled="okDisabled" @click="$emit('ok')">
          {{ ok }}
        </button>
      </div>
    </div>
  </BottomSheet>
  <Teleport v-else-if="mounted" to="body">
    <div :class="$attrs.class">
      <div class="ovl" :class="{ on }" @mousedown.self="close">
        <div ref="box" class="card modal" :class="{ wide }" role="dialog" aria-modal="true" :aria-labelledby="id" tabindex="-1">
          <div class="mh">
            <h2 :id="id">
              {{ title }}
            </h2>
            <button v-if="closable" type="button" class="xb" aria-label="Закрыть" @click="close">
              <svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
            </button>
          </div>
          <slot />
          <div v-if="ok" class="mf">
            <button type="button" class="pill" @click="close">
              Отмена
            </button>
            <button type="button" class="pill" :class="okClass ?? 'w'" :disabled="okDisabled" @click="$emit('ok')">
              {{ ok }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>
