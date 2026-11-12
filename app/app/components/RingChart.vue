<script setup lang="ts">
const props = defineProps<{ segs: { v: number, c: string }[] }>()
const R = 60, C = 2 * Math.PI * R
const arcs = computed(() => {
  const total = props.segs.reduce((s, x) => s + x.v, 0)
  let off = 0
  return props.segs.filter(s => total && s.v > 0).map((s) => {
    const len = C * s.v / total
    const arc = { c: s.c, dash: `${Math.max(0, len - 3)} ${C}`, off: -off }
    off += len
    return arc
  })
})
</script>

<template>
  <div class="relative mx-auto my-3 size-[150px]">
    <svg viewBox="0 0 150 150" class="size-full -rotate-90" aria-hidden="true">
      <circle cx="75" cy="75" :r="R" fill="none" stroke-width="16" style="stroke:rgba(var(--fg),.05)" />
      <circle
        v-for="(a, i) in arcs" :key="i" cx="75" cy="75" :r="R" fill="none" stroke-width="16"
        :stroke-dasharray="a.dash" :stroke-dashoffset="a.off" :style="{ stroke: a.c }" />
    </svg>
    <div class="absolute inset-0 grid place-content-center text-center">
      <slot />
    </div>
  </div>
</template>
