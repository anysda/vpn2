<script setup lang="ts">
defineEmits<{ back: [] }>()
</script>

<template>
  <button type="button" class="ib cbk" aria-label="Назад" @click="$emit('back')">
    <svg class="i" viewBox="0 0 24 24"><path d="m15 18-6-6 6-6" /></svg>
  </button>
</template>
