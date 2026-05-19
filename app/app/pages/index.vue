<script setup lang="ts">
useHead({ title: 'anysda-vpn2' })
</script>

<template>
  <div class="grid grid-cols-1 lg:grid-cols-[360px_minmax(0,1fr)] gap-6 max-w-[1600px] mx-auto">
    <div class="space-y-6">
      <UCard>
        <template #header>
          <div class="font-semibold">
            Клиенты
          </div>
        </template>
        <div class="text-(--ui-text-muted) text-sm py-4 text-center">
          (Phase A.next — список и CRUD клиентов)
        </div>
      </UCard>

      <UCard>
        <template #header>
          <div class="font-semibold">
            Боты
          </div>
        </template>
        <div class="text-(--ui-text-muted) text-sm py-4 text-center">
          (Phase D.4 — Telegram настройки)
        </div>
      </UCard>
    </div>

    <div class="space-y-6">
      <UCard>
        <template #header>
          <div class="font-semibold">
            Маршрутизация
          </div>
        </template>
        <div class="text-(--ui-text-muted) text-sm py-4 text-center">
          (Phase D.1 — drag-n-drop правил по outbound'ам)
        </div>
      </UCard>

      <UCard>
        <template #header>
          <div class="font-semibold">
            Мониторинг
          </div>
        </template>
        <div class="text-(--ui-text-muted) text-sm py-4 text-center">
          (Phase D.2 — карточки нод + RTT/Трафик графики)
        </div>
      </UCard>

      <UCard>
        <template #header>
          <div class="font-semibold">
            DNS
          </div>
        </template>
        <div class="text-(--ui-text-muted) text-sm py-4 text-center">
          (Phase D.3 — AdGuard мини-панель)
        </div>
      </UCard>
    </div>
  </div>
</template>
