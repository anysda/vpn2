<script setup lang="ts">
// Экран 3: Telegram-бот, можно пропустить. Токен - секрет: пишется в хранилище, назад не читается.
import { computed, ref } from 'vue'
import type { State } from '../api'

const props = defineProps<{ state: State }>()
const telegram = props.state.draft.telegram

const enabled = ref(telegram.enabled)
const chat = ref(telegram.chat_id)
const token = ref('')
const tokenSet = props.state.secrets.telegram_bot_token

const ready = computed(() => !enabled.value || ((tokenSet || !!token.value.trim()) && !!chat.value.trim()))
defineExpose({
  ready,
  collect: () => ({
    patch: { telegram: { enabled: enabled.value, chat_id: enabled.value ? chat.value.trim() : '' } },
    secrets: enabled.value && token.value.trim() ? { telegram_bot_token: token.value.trim() } : {},
  }),
})
</script>

<template>
  <section>
    <div class="fld wzsw">
      <span>Telegram-бот</span>
      <code>{{ enabled ? 'включён' : 'пропущен - бота можно подключить позже в панели' }}</code>
      <button type="button" class="tog" :class="{ on: enabled }" role="switch" :aria-checked="enabled" aria-label="Telegram-бот" @click="enabled = !enabled"><i /></button>
    </div>
    <template v-if="enabled">
      <label class="fl2">
        <span>Токен бота от @BotFather</span>
        <input v-model="token" class="in mono" type="password" autocomplete="off" :placeholder="tokenSet ? 'задан - оставьте пустым, чтобы не менять' : '123456:ABC…'">
      </label>
      <label class="fl2">
        <span>Chat ID администратора</span>
        <input v-model="chat" class="in mono" inputmode="numeric" autocomplete="off" placeholder="123456789">
      </label>
    </template>
  </section>
</template>
