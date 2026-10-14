<script setup lang="ts">
// Спарклайн карточки сводки: кривая с мягкой заливкой, как line() в макете.
const props = withDefaults(defineProps<{ data: number[], col?: string }>(), { col: 'var(--acc)' })
const W = 200, H = 40
const id = `sp${useId()}`.replace(/[^\w-]/g, '')
const d = computed(() => smooth(sparkPoints(props.data, W, H, 3)))
</script>

<template>
  <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" aria-hidden="true">
    <defs>
      <linearGradient :id="id" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" style="stop-color:var(--acc)" stop-opacity=".08" />
        <stop offset="1" style="stop-color:var(--acc)" stop-opacity="0" />
      </linearGradient>
    </defs>
    <path :d="`${d} L${W},${H} L0,${H} Z`" :fill="`url(#${id})`" />
    <path :d="d" fill="none" :style="{ stroke: col }" stroke-width="1.4" vector-effect="non-scaling-stroke" />
  </svg>
</template>
