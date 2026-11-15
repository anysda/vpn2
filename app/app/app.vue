<script setup lang="ts">
import { ru } from '@nuxt/ui/locale'

const mobile = useMobile()
// Chromium считает фокус <select> после клика мышью видимым: рамку выбора рисуем только после Tab (clients.css).
onMounted(() => {
  const root = document.documentElement
  window.addEventListener('keydown', (e) => { if (e.key === 'Tab') root.dataset.kbd = '' }, true)
  window.addEventListener('pointerdown', () => { delete root.dataset.kbd }, true)
  // Окно стоит по центру экрана, его шапка - на любой высоте: всплывашки телефона встают под неё (panel.css).
  const heads = new ResizeObserver(() => {
    const head = [...document.querySelectorAll('[role="dialog"] [data-slot="header"]')].pop()
    if (head) root.style.setProperty('--dlg-head', `${Math.round(head.getBoundingClientRect().bottom) + 10}px`)
  })
  new MutationObserver(() => {
    heads.disconnect()
    document.querySelectorAll('[role="dialog"]').forEach(dialog => heads.observe(dialog))
  }).observe(document.body, { childList: true, subtree: true })
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
