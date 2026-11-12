<script setup lang="ts" generic="T extends string">
defineProps<{ options: ReadonlyArray<{ value: T, label: string }>, small?: boolean }>()
const model = defineModel<T>({ required: true })
</script>

<template>
  <div class="seg" :class="{ sm: small }" role="tablist">
    <button
      v-for="o in options" :key="o.value" type="button" role="tab" :class="{ on: o.value === model }" :aria-selected="o.value === model"
      @click="model = o.value">
      {{ o.label }}
    </button>
  </div>
</template>
