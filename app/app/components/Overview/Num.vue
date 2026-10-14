<script setup lang="ts">
import { count, roll, useWake } from '~/utils/overview'

// Число обзора. Текстом владеет DOM, а не Vue: первое значение в сцене
// пробуждения считается с нуля, дальше меняются только разряды (перекат).
// tag — каким элементом стать: в макете число само и есть .big или b
const props = defineProps<{ value: string, dur?: number, delay?: number, tag?: string }>()
const el = ref<HTMLElement>()
const wake = useWake()
let shown = false

function apply(v: string) {
  const e = el.value
  if (!e) return
  if (shown) return roll(e, v)
  e.textContent = v
  if (!/\d/.test(v)) return
  shown = true
  if (wake.on && props.dur) count(e, props.dur, wake.at(props.delay ?? 0))
}

onMounted(() => apply(props.value))
watch(() => props.value, apply)
</script>

<template>
  <component :is="tag ?? 'span'" ref="el" />
</template>
