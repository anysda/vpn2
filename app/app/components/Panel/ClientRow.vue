<script setup lang="ts">
/** Строка списка клиентов: на телефоне аватар отдельной колонкой, на десктопе внутри .who. */
export interface ClientDot { cls: string, title: string, color?: string }
defineProps<{ name: string, initials: string, selected: boolean, mobile: boolean, dot: ClientDot | null }>()
defineEmits<{ select: [] }>()
</script>

<template>
  <div
    class="crow"
    :class="{ on: !mobile && selected }"
    role="button"
    tabindex="0"
    :aria-pressed="selected"
    data-testid="client-row"
    @click="$emit('select')"
    @keydown.enter.prevent="$emit('select')"
    @keydown.space.prevent="$emit('select')"
  >
    <span v-if="mobile" class="av" :title="dot?.title">{{ initials }}<i
      v-if="dot"
      :class="dot.cls"
      :style="dot.color ? { background: dot.color } : undefined"
      data-testid="client-online"
    /></span>
    <div class="who">
      <template v-if="mobile">
        <b><span>{{ name }}</span><slot name="icons" /></b>
        <small><slot name="meta" /></small>
      </template>
      <template v-else>
        <span class="av" :title="dot?.title">{{ initials }}<i
          v-if="dot"
          :class="dot.cls"
          :style="dot.color ? { background: dot.color } : undefined"
          data-testid="client-online"
        /></span>
        <div>
          <b><span>{{ name }}</span><slot name="icons" /></b>
          <small><slot name="meta" /></small>
        </div>
      </template>
    </div>
    <slot />
  </div>
</template>
