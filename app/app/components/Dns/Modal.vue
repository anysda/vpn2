<script setup lang="ts">
// Окно макета: затемнение с размытием, карточка въезжает, строки — лесенкой.
// В body, чтобы position:fixed не ломался под анимированными предками.
const open = defineModel<boolean>('open', { required: true })
defineProps<{ title: string, closeBtn?: boolean }>()

const box = ref<HTMLElement>()
const id = useId()

function close() {
  open.value = false
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && open.value) close()
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

onMounted(() => addEventListener('keydown', onKey))
onUnmounted(() => removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
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
            <slot name="foot" :close="close" />
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>
