<script setup lang="ts">
useHead({ title: 'Настройки — anysda-vpn2' })
definePageMeta({ title: 'Настройки', sub: 'Экзиты, бот, профиль и вход в панель' })
</script>

<template>
  <main class="tab on stg">
    <div class="head">
      <div>
        <h1>Настройки</h1>
        <p>Экзиты, бот, профиль и вход в панель</p>
      </div>
    </div>
    <section class="sgrid">
      <SettingsProfile />
      <BotsSection />
      <SettingsSso />
      <SettingsExits />
    </section>
  </main>
</template>
