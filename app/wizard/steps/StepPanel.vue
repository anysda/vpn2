<script setup lang="ts">
// Экран 2: домен панели или вход по IP, учётка администратора. Пароль уходит в хранилище
// сервера и больше не показывается: сервер говорит только «задан».
import { computed, ref } from 'vue'
import type { State } from '../api'

const props = defineProps<{ state: State }>()
const panel = props.state.draft.panel

const byDomain = ref(panel.domain !== null)
const domain = ref(panel.domain ?? '')
const user = ref(panel.admin_user)
const password = ref('')
const reveal = ref(false)
const passwordSet = props.state.secrets.admin_password

const ready = computed(() => (!byDomain.value || !!domain.value.trim()) && !!user.value.trim() && (passwordSet || !!password.value))
defineExpose({
  ready,
  collect: () => ({
    patch: { panel: { domain: byDomain.value ? domain.value.trim() : null, admin_user: user.value.trim() } },
    secrets: password.value ? { admin_password: password.value } : {},
  }),
})
</script>

<template>
  <section>
    <div class="seg sm" role="group" aria-label="Адрес панели">
      <button type="button" :class="{ on: !byDomain }" @click="byDomain = false">По IP сервера</button>
      <button type="button" :class="{ on: byDomain }" @click="byDomain = true">Свой домен</button>
    </div>
    <p v-if="!byDomain" class="mt">Панель откроется по адресу этого сервера. Домен можно указать позже в config.yaml.</p>
    <label v-else class="fl2">
      <span>Домен. A-запись должна указывать на IP этого сервера, только IPv4 (без AAAA)</span>
      <input v-model="domain" class="in mono" placeholder="vpn.example.com" autocomplete="off" spellcheck="false">
    </label>
    <label class="fl2">
      <span>Логин администратора</span>
      <input v-model="user" class="in" autocomplete="username" autocapitalize="none" spellcheck="false">
    </label>
    <label class="fl2">
      <span>Пароль администратора</span>
      <span class="wzpw">
        <input
          v-model="password" class="in" :type="reveal ? 'text' : 'password'" autocomplete="new-password"
          :placeholder="passwordSet ? 'задан - оставьте пустым, чтобы не менять' : ''">
        <button type="button" class="pill" :aria-pressed="reveal" @click="reveal = !reveal">{{ reveal ? 'Скрыть' : 'Показать' }}</button>
      </span>
    </label>
  </section>
</template>
