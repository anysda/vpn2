<script setup lang="ts">
// Переключатель макета. Нажатие и disabled приходят снаружи как обычные атрибуты кнопки.
defineProps<{ on: boolean, label: string }>()
</script>

<template>
  <button type="button" class="tog" :class="{ on }" role="switch" :aria-checked="on" :aria-label="label">
    <i />
  </button>
</template>
