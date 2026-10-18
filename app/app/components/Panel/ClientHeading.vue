<script setup lang="ts">
defineProps<{ initials: string, subtitle: string, status?: string, statusClass?: string }>()
</script>

<template>
  <div class="cdh">
    <span class="av">{{ initials }}</span>
    <div style="min-width:0">
      <slot />
      <small>{{ subtitle }}</small>
    </div>
    <span v-if="status" class="tag" :class="statusClass">{{ status }}</span>
  </div>
</template>
