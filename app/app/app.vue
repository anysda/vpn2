<script setup lang="ts">
import { ru } from '@nuxt/ui/locale'

const mobile = useMobile()
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
