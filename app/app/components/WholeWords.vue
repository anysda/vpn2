<script setup lang="ts">
// Текст по словам: слово, которое помещается в строку, переносится только целиком, шире
// строки - по слогам с дефисом (словарь по lang документа). Разрыв без дефиса - лишь когда
// в строку не входит и слог: иначе текст вылез бы за свой блок.
defineProps<{ text: string }>()
</script>

<template>
  <template v-for="(w, i) in text.split(' ')" :key="i">{{ i ? ' ' : '' }}<span class="wd">{{ w }}</span></template>
</template>

<style>
.wd{display:inline-block;-webkit-hyphens:auto;hyphens:auto;overflow-wrap:anywhere}
</style>
