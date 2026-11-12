<script setup lang="ts">
defineProps<{ title: string, ok?: string, danger?: boolean, busy?: boolean, wide?: boolean }>()
defineEmits<{ ok: [] }>()
const open = defineModel<boolean>('open', { default: false })
useLayer(open)

// Курсор - в первое поле окна, а не на крестик; поле, выбранное раньше, не отнимается.
function firstField(e: Event) {
  const box = e.target as HTMLElement
  const field = box.querySelector<HTMLElement>('input, textarea, select')
  if (!field) return
  e.preventDefault()
  if (!box.contains(document.activeElement)) field.focus()
}
</script>

<template>
  <UModal v-model:open="open" :title="title" :content="{ onOpenAutoFocus: firstField }" :ui="{ content: `clm ${wide ? 'sm:max-w-2xl' : 'sm:max-w-md'}`, footer: 'justify-end' }">
    <template #body>
      <slot />
    </template>
    <template #footer>
      <slot name="footer">
        <button type="button" class="pill" @click="open = false">Отмена</button>
        <button v-if="ok" type="button" class="pill" :class="danger ? 'redw' : 'w'" :disabled="busy" @click="$emit('ok')">
          {{ ok }}
        </button>
      </slot>
    </template>
  </UModal>
</template>
