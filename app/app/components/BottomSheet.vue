<script setup lang="ts">
// Нижняя шторка телефона: меню профиля, уведомления и мобильные окна
// разделов. Закрывается тапом по фону и Escape; кнопки — в слоте foot.
const props = defineProps<{ open: boolean, label: string }>()
const emit = defineEmits<{ close: [] }>()
const panel = ref<HTMLElement>()

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}
function lock(on: boolean) {
  document.documentElement.style.overflow = on ? 'hidden' : ''
  if (on) addEventListener('keydown', onKey)
  else removeEventListener('keydown', onKey)
}
watch(() => props.open, (on) => {
  lock(on)
  if (on) nextTick(() => panel.value?.focus({ preventScroll: true }))
})
onMounted(() => { if (props.open) lock(true) })
onUnmounted(() => { if (props.open) lock(false) })
</script>

<template>
  <Teleport to="#teleports">
    <div class="bso" :class="{ on: open }" aria-hidden="true" @click="emit('close')" />
    <div
      ref="panel"
      class="bsh"
      :class="{ on: open }"
      role="dialog"
      aria-modal="true"
      :aria-label="label"
      :aria-hidden="!open"
      :inert="!open"
      tabindex="-1"
    >
      <slot />
      <div v-if="$slots.foot" class="bsf">
        <slot name="foot" />
      </div>
    </div>
  </Teleport>
</template>
