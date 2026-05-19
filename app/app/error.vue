<script setup lang="ts">
defineProps<{ error: { statusCode?: number, message?: string, statusMessage?: string } }>()

function handleError() {
  clearError({ redirect: '/' })
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-(--ui-bg) p-4">
    <UCard class="w-full max-w-md">
      <template #header>
        <div class="flex items-center gap-2 font-semibold text-lg">
          <UIcon name="i-lucide-triangle-alert" class="text-red-500" />
          Ошибка
          <span v-if="error.statusCode" class="text-(--ui-text-muted) text-sm">
            ({{ error.statusCode }})
          </span>
        </div>
      </template>

      <div class="space-y-3 text-sm">
        <p class="text-(--ui-text-muted)">
          {{ error.statusMessage || error.message || 'Что-то пошло не так.' }}
        </p>
        <div class="flex gap-2">
          <UButton color="primary" @click="handleError">
            На главную
          </UButton>
          <UButton
            color="neutral"
            variant="soft"
            @click="$router.go(0)"
          >
            Обновить
          </UButton>
        </div>
      </div>
    </UCard>
  </div>
</template>
