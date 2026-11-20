<script setup lang="ts">
// Экран 6: всё введённое одним списком, у каждого раздела «Изменить». Секреты - только «задан».
import { computed } from 'vue'
import { login, type State } from '../api'

const props = defineProps<{ state: State }>()
const emit = defineEmits<{ go: [screen: number] }>()

const rows = computed(() => {
  const { draft: d, secrets: s } = props.state
  const key = d.admin_ssh_key.trim().split(/\s+/)
  return [
    { screen: 1, title: 'Экзиты', lines: d.exits.map(e => `${e.tag.toUpperCase()} · ${login(e.host)}${e.skipped ? ' · без него' : ''}`) },
    { screen: 1, title: 'Ваш ключ ssh', lines: [`${key[0]} …${(key[1] ?? '').slice(-12)}${key[2] ? ` ${key[2]}` : ''}`] },
    { screen: 2, title: 'Панель', lines: [d.panel.domain ? `https://${d.panel.domain}` : 'по IP сервера', `логин ${d.panel.admin_user}, пароль ${s.admin_password ? 'задан' : 'не задан'}`] },
    { screen: 3, title: 'Telegram-бот', lines: [d.telegram.enabled ? `включён, chat ID ${d.telegram.chat_id}, токен ${s.telegram_bot_token ? 'задан' : 'не задан'}` : 'пропущен'] },
    { screen: 4, title: 'Бэкапы', lines: [d.backup.target === 's3' ? `S3: ${d.backup.s3.bucket} на ${d.backup.s3.endpoint}` : 'на этом сервере', `ключ ${props.state.backup_key === 'seen' ? 'сохранён' : 'не сохранён'}`] },
    { screen: 5, title: 'YouTube через РФ', lines: [d.youtube ? 'включено' : 'выключено'] },
  ]
})
defineExpose({ ready: true, collect: () => ({}) })
</script>

<template>
  <section>
    <div v-for="row in rows" :key="row.title" class="fld wzsum">
      <span>{{ row.title }}</span>
      <code><span v-for="line in row.lines" :key="line">{{ line }}</span></code>
      <button type="button" class="pill" @click="emit('go', row.screen)">Изменить</button>
    </div>
    <p class="mt">«Установить» запускает установку на всех серверах. Вкладку можно закрыть: установка пойдёт дальше, ссылка вернёт на её ход.</p>
  </section>
</template>
