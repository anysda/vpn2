<script setup lang="ts">
// Первая загрузка подраздела не удалась: причина и повтор без перезагрузки страницы.
defineProps<{ of: { data: Ref<unknown>, error: Ref<unknown>, status: Ref<string>, refresh: () => unknown } }>()
</script>

<template>
  <div v-if="of.error.value && !of.data.value" class="card lerr">
    <h3 role="alert">
      {{ errText(of.error.value) }}
    </h3>
    <p class="mt">
      <button type="button" class="pill" :disabled="of.status.value === 'pending'" @click="of.refresh()">
        Повторить загрузку
      </button>
    </p>
  </div>
</template>
