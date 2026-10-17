<script setup lang="ts">
// На телефоне тосты сверху, под шапкой (main.css).
const mobile = useMobile()
useHead({
  link: [
    { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' },
  ],
})
</script>

<template>
  <UApp :toaster="{ position: mobile ? 'top-center' : 'bottom-right' }">
    <NuxtLayout>
      <NuxtPage />
    </NuxtLayout>
  </UApp>
</template>
