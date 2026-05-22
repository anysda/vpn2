<script setup lang="ts">
/**
 * Поле ввода даты в формате ДД.ММ.ГГГГ с авто-маской: точки расставляются
 * сами по мере набора цифр, вручную их вводить не нужно.
 */
const model = defineModel<string>({ default: '' })

/** Цифры из ввода → строка ДД.ММ.ГГГГ. Точки — только между группами,
 *  без хвостовой: иначе backspace «залипает» на точке. */
function maskRuDate(raw: string): string {
  const d = raw.replace(/\D/g, '').slice(0, 8)
  if (d.length <= 2) return d
  if (d.length <= 4) return `${d.slice(0, 2)}.${d.slice(2)}`
  return `${d.slice(0, 2)}.${d.slice(2, 4)}.${d.slice(4)}`
}

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
