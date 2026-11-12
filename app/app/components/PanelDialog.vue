<script setup lang="ts">
// Окно панели: заголовок, содержимое, внизу «Отмена» и главное действие `ok`.
defineProps<{ title: string, ok?: string, danger?: boolean, busy?: boolean, wide?: boolean }>()
defineEmits<{ ok: [] }>()
const open = defineModel<boolean>('open', { default: false })
</script>

<template>
  <UModal v-model:open="open" :title="title" :ui="{ content: `clm ${wide ? 'sm:max-w-2xl' : 'sm:max-w-md'}`, footer: 'justify-end' }">
    <template #body>
      <slot />
    </template>
    <template #footer>
      <slot name="footer">
        <button type="button" class="pill" @click="open = false">
          Отмена
        </button>
        <button v-if="ok" type="button" class="pill" :class="danger ? 'redw' : 'w'" :disabled="busy" @click="$emit('ok')">
          {{ ok }}
        </button>
      </slot>
    </template>
  </UModal>
</template>
