<script setup lang="ts">

const { status: data, refresh } = useTelegramStatus(10_000)

const toast = useToast()

const newToken = ref('')
const newChatId = ref('')
const newAdminUsername = ref('')
const saving = ref(false)

watch(data, (d, previous) => {
  if (d) {
    // Поздний первый GET и поллер обновляют статус, но не черновик пользователя.
    if (newChatId.value === String(previous?.chat_id ?? '')) newChatId.value = String(d.chat_id ?? '')
    if (newAdminUsername.value === String(previous?.admin_username ?? '')) newAdminUsername.value = String(d.admin_username ?? '')
  }
}, { immediate: true })

const status = computed(() => {
  if (!data.value?.configured) return { label: 'Не настроен', cls: '' }
  if (data.value.running) return { label: 'Онлайн', cls: 'ok' }
  return { label: 'Офлайн', cls: 'badt' }
})
const showToken = ref(false)

const dirty = computed(() =>
  !!newToken.value.trim()
  || newChatId.value.trim() !== String(data.value?.chat_id ?? '')
  || newAdminUsername.value.trim() !== String(data.value?.admin_username ?? ''),
)

async function save() {
  saving.value = true
  const submitted = { token: newToken.value, chatId: newChatId.value, username: newAdminUsername.value }
  try {
    const body: Record<string, string> = {}
    if (newToken.value.trim()) body.bot_token = newToken.value.trim()
    if (newChatId.value.trim() !== String(data.value?.chat_id ?? '')) {
      body.chat_id = newChatId.value.trim()
    }
    if (newAdminUsername.value.trim() !== String(data.value?.admin_username ?? '')) {
      body.admin_username = newAdminUsername.value.trim()
    }
    if (Object.keys(body).length === 0) {
      saving.value = false
      return
    }
    await $fetch('/api/admin/telegram', { method: 'PUT', body })
    toast.add({ title: 'Настройки бота сохранены', color: 'success' })
    // Не стираем следующую правку, сделанную, пока PUT был в пути.
    if (newToken.value === submitted.token) newToken.value = ''
    if (newChatId.value === submitted.chatId) newChatId.value = submitted.chatId.trim()
    if (newAdminUsername.value === submitted.username) newAdminUsername.value = submitted.username.trim().replace(/^@+/, '')
    // Опрос в пути начат до записи: его ответ устарел, нужен свежий запрос.
    await refresh({ dedupe: 'cancel' })
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: 'Ошибка', color: 'error', description: err.statusMessage })
  }
  finally {
    saving.value = false
  }
}

</script>

<template>
  <div class="card sc sbot">
    <div class="sh">
      <h3>Бот Telegram</h3>
      <span class="tag" :class="status.cls"><span v-if="status.cls" class="dot" :style="{ background: status.cls === 'ok' ? 'var(--green)' : 'var(--red)' }" />{{ status.label }}</span>
    </div>
    <div class="sbig">
      <span class="big">{{ data?.username ? `@${data.username}` : '—' }}</span>
    </div>
    <div class="ys">
      <div><span>Подписаны</span><b>{{ data?.clients_subscribed ?? 0 }} из {{ data?.clients_total ?? 0 }}</b></div>
      <div><span>В очереди</span><b>{{ data?.queued ?? 0 }}</b></div>
      <div><span>Доставлено за сутки</span><b>{{ data?.delivered_day ?? 0 }}</b></div>
    </div>
    <div class="fg2">
      <label class="fld"><span>Token</span><input
        v-model="newToken" class="in mono" :type="showToken ? 'text' : 'password'" autocomplete="off" data-1p-ignore data-lpignore="true"
        data-bwignore :placeholder="data?.bot_token_masked || ''"><button type="button" class="xb eye" aria-label="Показать" :aria-pressed="showToken" @click="showToken = !showToken"><svg class="i" viewBox="0 0 24 24"><path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6S2 12 2 12z" /><circle cx="12" cy="12" r="2.6" /></svg></button></label>
      <label class="fld"><span>Chat ID</span><input
        v-model="newChatId" class="in mono" autocomplete="off" data-1p-ignore data-lpignore="true" data-bwignore inputmode="numeric"></label>
      <label class="fld"><span>Никнейм админа бота</span><input
        v-model="newAdminUsername" class="in" autocomplete="off" data-1p-ignore data-lpignore="true" data-bwignore placeholder="@ник"></label>
      <div class="fld">
        <span />
        <button type="button" class="pill w" :disabled="!dirty || saving" @click="save">Сохранить</button>
      </div>
    </div>
  </div>
</template>
