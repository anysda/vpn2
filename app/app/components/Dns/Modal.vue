<script setup lang="ts">
// Окно макета: затемнение с размытием, карточка въезжает, строки — лесенкой.
// В body, чтобы position:fixed не ломался под анимированными предками.
// На телефоне то же содержимое — в общей нижней шторке. Внизу всегда «Отмена»
// и одно действие.
const open = defineModel<boolean>('open', { required: true })
defineProps<{ title: string, closeBtn?: boolean, ok: string, okClass?: string, okDisabled?: boolean }>()
defineEmits<{ ok: [] }>()

const box = ref<HTMLElement>()
const id = useId()
const mobile = useMobile()

function close() {
  open.value = false
}

watch(open, async (v) => {
  if (!v) return
  await nextTick()
  const el = box.value
  if (!el) return
  ;[...el.children].slice(0, 6).forEach((c, i) =>
    anim(c, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], { duration: 300, delay: 40 + i * 35 }))
  // Курсор — в первое поле, если человек ещё не выбрал поле сам: иначе набранное
  // уйдёт не туда.
  setTimeout(() => {
    if (!el.contains(document.activeElement)) el.querySelector<HTMLInputElement>('input:not([readonly])')?.focus()
  }, 60)
})

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && open.value && !mobile.value) close()
}
onMounted(() => addEventListener('keydown', onKey))
onUnmounted(() => removeEventListener('keydown', onKey))
</script>

<template>
  <BottomSheet v-if="mobile" :open="open" :label="title" @close="close">
    <div ref="box" class="dns dsh">
      <div class="mh">
        <h2>{{ title }}</h2>
        <button v-if="closeBtn" type="button" class="xb" aria-label="Закрыть" @click="close">
          <svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
        </button>
      </div>
      <slot />
      <div class="bsf">
        <button type="button" class="pill" @click="close">
          Отмена
        </button>
        <button type="button" class="pill" :class="okClass ?? 'w'" :disabled="okDisabled" @click="$emit('ok')">
          {{ ok }}
        </button>
      </div>
    </div>
  </BottomSheet>
  <Teleport v-else to="body">
    <div class="dns">
      <div class="ovl" :class="{ on: open }" @mousedown.self="close">
        <div ref="box" class="card modal" role="dialog" aria-modal="true" :aria-labelledby="id" :inert="!open">
          <div class="mh">
            <h2 :id="id">
              {{ title }}
            </h2>
            <button v-if="closeBtn" type="button" class="xb" aria-label="Закрыть" @click="close">
              <svg class="i" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
            </button>
          </div>
          <slot />
          <div class="mf">
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
