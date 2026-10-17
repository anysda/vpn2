<script setup lang="ts">
// Первая загрузка подраздела не удалась: причина и повтор без перезагрузки страницы.
defineProps<{ message: string, pending?: boolean }>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="card lerr">
    <h3 role="alert">
      {{ message }}
    </h3>
    <p class="mt">
      <button type="button" class="pill" :disabled="pending" @click="$emit('retry')">
        Повторить загрузку
      </button>
    </p>
  </div>
</template>
