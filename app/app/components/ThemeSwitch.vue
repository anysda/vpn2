<script setup lang="ts">
const colorMode = useColorMode()
// Выбор известен только в браузере: до монтирования ни одна кнопка не нажата.
const mounted = ref(false)
onMounted(() => { mounted.value = true })

const THEMES = [
  { value: 'system', label: 'Как в системе' },
  { value: 'light', label: 'Светлая' },
  { value: 'dark', label: 'Тёмная' },
] as const

let timer: ReturnType<typeof setTimeout> | undefined
function choose(value: string) {
  const html = document.documentElement
  html.classList.add('tt')
  colorMode.preference = value
  clearTimeout(timer)
  timer = setTimeout(() => html.classList.remove('tt'), 350)
}
</script>

<template>
  <div class="thm" role="group" aria-label="Тема">
    <span>Тема</span>
    <button
      v-for="t in THEMES"
      :key="t.value"
      type="button"
      :title="t.label"
      :aria-label="t.label"
      :aria-pressed="mounted && colorMode.preference === t.value"
      @click="choose(t.value)"
    >
      <svg v-if="t.value === 'system'" class="i" viewBox="0 0 24 24"><rect x="3" y="4.5" width="18" height="12" rx="2" /><path d="M9 20h6M12 16.5V20" /></svg>
      <svg v-else-if="t.value === 'light'" class="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4" /><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4" /></svg>
      <svg v-else class="i" viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" /></svg>
    </button>
  </div>
</template>
