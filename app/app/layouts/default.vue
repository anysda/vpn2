<script setup lang="ts">
const { user, clear } = useUserSession()
const colorMode = useColorMode()

function toggleTheme() {
  colorMode.preference = colorMode.value === 'dark' ? 'light' : 'dark'
}

async function logout() {
  try {
    await $fetch('/api/auth/logout', { method: 'POST' })
  }
  finally {
    await clear()
    await navigateTo('/login')
  }
}

const menuItems = computed(() => [
  [{ label: 'Мой профиль', icon: 'i-lucide-user', to: '/me' }],
  [{ label: 'Выйти', icon: 'i-lucide-log-out', onSelect: logout, color: 'error' as const }],
])
</script>

<template>
  <div class="min-h-screen bg-(--ui-bg) text-(--ui-text)">
    <header class="border-b border-(--ui-border) px-6 py-3 flex items-center justify-between">
      <NuxtLink to="/" class="font-semibold text-lg flex items-center gap-2">
        <UIcon name="i-lucide-shield" class="text-(--ui-primary)" />
        anysda-vpn2
      </NuxtLink>
      <div class="flex items-center gap-1">
        <ClientOnly>
          <UButton
            :icon="colorMode.value === 'dark' ? 'i-lucide-moon' : 'i-lucide-sun'"
            color="neutral"
            variant="ghost"
            @click="toggleTheme"
          />
          <template #fallback>
            <UButton icon="i-lucide-loader" color="neutral" variant="ghost" disabled />
          </template>
        </ClientOnly>
        <UDropdownMenu :items="menuItems">
          <UButton
            color="neutral"
            variant="ghost"
            trailing-icon="i-lucide-chevron-down"
          >
            @{{ user?.username ?? 'admin' }}
          </UButton>
        </UDropdownMenu>
      </div>
    </header>
    <main class="p-6">
      <slot />
    </main>
  </div>
</template>
