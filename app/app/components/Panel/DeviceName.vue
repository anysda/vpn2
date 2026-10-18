<script setup lang="ts">
defineProps<{ name: string }>()
</script>

<template>
  <div>
    <b>{{ name }}</b>
    <slot />
  </div>
</template>
