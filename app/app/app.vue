<script setup lang="ts">
import { ru } from '@nuxt/ui/locale'

const mobile = useMobile()
// Chromium считает фокус <select> после клика мышью видимым: рамку выбора рисуем только после Tab (clients.css).
onMounted(() => {
  const root = document.documentElement
  window.addEventListener('keydown', (e) => { if (e.key === 'Tab') root.dataset.kbd = '' }, true)
  window.addEventListener('pointerdown', () => { delete root.dataset.kbd }, true)
})
useHead({
  link: [
    { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' },
  ],
})
</script>

<template>
  <UApp :locale="ru" :toaster="{ position: mobile ? 'top-center' : 'bottom-right' }">
    <NuxtLayout>
      <NuxtPage />
    </NuxtLayout>
  </UApp>
</template>
