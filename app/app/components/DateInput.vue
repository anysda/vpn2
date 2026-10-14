<script setup lang="ts">
/**
 * Поле ввода даты в формате ДД.ММ.ГГГГ с авто-маской: точки расставляются
 * сами по мере набора цифр, вручную их вводить не нужно.
 */
import { maskRuDate } from '~/utils/ru-date-mask'

const model = defineModel<string>({ default: '' })

function onInput(e: Event) {
  const el = e.target as HTMLInputElement
  const masked = maskRuDate(el.value)
  // Правим DOM сразу — не дожидаясь реактивности: иначе при «пустой»
  // маске (ввод нецифры) поле сохранило бы лишний символ.
  el.value = masked
  model.value = masked
}
</script>

<template>
  <UInput
    :model-value="model"
    placeholder="ДД.ММ.ГГГГ"
    inputmode="numeric"
    maxlength="10"
    @input="onInput"
  />
</template>
