<script setup lang="ts">
defineProps<{ label: string, disabled?: boolean }>()
const on = defineModel<boolean>({ required: true })
</script>

<template>
  <button type="button" role="switch" class="tog" :class="{ on }" :aria-checked="on" :aria-label="label" :disabled="disabled" @click="on = !on">
    <i />
  </button>
</template>
