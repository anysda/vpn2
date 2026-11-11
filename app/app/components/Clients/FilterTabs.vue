<script setup lang="ts">
import type { RouteLocationRaw } from 'vue-router'

export type Filter = 'all' | 'exp' | 'frz'

/** Сегменты фильтра списка клиентов: кнопки - ссылки, адрес каждой даёт страница. */
const props = defineProps<{ filter: Filter, counts: Record<Filter, number>, to: (f: Filter) => RouteLocationRaw }>()

/** Пробел нажимает ссылку-кнопку, как настоящую кнопку. */
const press = (e: KeyboardEvent) => (e.currentTarget as HTMLElement).click()

// Подложка активного сегмента ездит за кнопкой, как в шапке.
const seg = ref<HTMLElement>()
const glided = ref(false)
function glide(jump = false) {
  if (placeGlide(seg.value, glided.value, jump)) glided.value = true
}
const onResize = () => glide(true)
watch(() => props.filter, () => nextTick(() => requestAnimationFrame(() => glide())))
watch(() => props.counts, () => nextTick(() => glide(true)))
onMounted(() => {
  nextTick(() => glide(true))
  addEventListener('resize', onResize)
  void document.fonts?.ready.then(() => glide(true))
})
onUnmounted(() => removeEventListener('resize', onResize))
</script>

<template>
  <div ref="seg" class="seg gl">
    <NuxtLink
      v-for="[f, label] in ([['all', 'Все'], ['exp', 'Истекают'], ['frz', 'Заморожены']] as const)" :key="f" :to="to(f)" replace role="button"
      :aria-pressed="filter === f" :aria-current="undefined" :class="{ on: filter === f }" @keydown.space.prevent="press">
      {{ label }} <b>{{ counts[f] }}</b>
    </NuxtLink>
    <span class="glide now" />
  </div>
</template>
