<script setup lang="ts">
defineProps<{ title: string, sub: string }>()
</script>

<template>
  <div class="fhead">
    <div>
      <h3>{{ title }}</h3>
      <p class="sub2">{{ sub }}</p>
    </div>
    <slot />
  </div>
</template>
