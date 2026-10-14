<script setup lang="ts">
import { pluralRu } from '#shared/plural'
import { ROUTES_KEY } from '~/composables/useRoutes'
import type { Route } from '~/composables/useRoutes'

useHead({ title: 'Маршруты — anysda-vpn2' })

const { data: rules } = useNuxtData<Route[]>(ROUTES_KEY)
const count = computed(() => {
  const n = rules.value?.length ?? 0
  return `${n} ${pluralRu(n, ['правило', 'правила', 'правил'])}`
})
</script>

<template>
  <main class="tab on">
    <div class="head">
      <div>
        <h1>Маршруты</h1>
        <p>{{ count }} · какой сайт через какой выход</p>
      </div>
    </div>
    <RoutesSection />
  </main>
</template>
