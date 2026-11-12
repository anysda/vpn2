<script setup lang="ts" generic="T extends string">
defineProps<{ options: ReadonlyArray<{ value: T, label: string }>, small?: boolean }>()
const model = defineModel<T>({ required: true })
// Выбранная вкладка целиком на виду, когда ряд листается вбок.
function pick(v: T, e: MouseEvent) {
  model.value = v
  const tab = e.currentTarget as HTMLElement
  tab.scrollIntoView({ block: 'nearest', inline: 'nearest' })
}
</script>

<template>
  <div class="seg" :class="{ sm: small }" role="tablist">
    <button
      v-for="o in options" :key="o.value" type="button" role="tab" :class="{ on: o.value === model }" :aria-selected="o.value === model"
      @click="pick(o.value, $event)">
      {{ o.label }}
    </button>
  </div>
</template>
