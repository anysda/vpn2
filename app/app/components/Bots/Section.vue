<script setup lang="ts">
const { status: data, refresh } = useTelegramStatus(10_000)
const { busy, act } = useAct()
const token = ref('')
const chatId = ref('')
const admin = ref('')
const showToken = ref(false)
const savedChat = () => String(data.value?.chat_id ?? '')
const savedAdmin = () => String(data.value?.admin_username ?? '')

// Опрос обновляет поле, только пока оператор его не правил.
watch(data, (d, prev) => {
  if (!d) return
  if (chatId.value === String(prev?.chat_id ?? '')) chatId.value = String(d.chat_id ?? '')
  if (admin.value === String(prev?.admin_username ?? '')) admin.value = String(d.admin_username ?? '')
}, { immediate: true })

const status = computed(() => !data.value?.configured ? { label: 'Не настроен', cls: '' } : data.value.running ? { label: 'Онлайн', cls: 'ok' } : { label: 'Офлайн', cls: 'badt' })
const body = computed(() => {
  const b: Record<string, string> = {}
  if (token.value.trim()) b.bot_token = token.value.trim()
  if (chatId.value.trim() !== savedChat()) b.chat_id = chatId.value.trim()
  if (admin.value.trim() !== savedAdmin()) b.admin_username = admin.value.trim()
  return b
})

async function save() {
  const sent = { token: token.value, chatId: chatId.value, admin: admin.value }
  if (!Object.keys(body.value).length) return
  await act('save', async () => {
    await $fetch<unknown>('/api/admin/telegram', { method: 'PUT', body: body.value })
    // Правку, сделанную пока PUT был в пути, не стираем.
    if (token.value === sent.token) token.value = ''
    if (chatId.value === sent.chatId) chatId.value = sent.chatId.trim()
    if (admin.value === sent.admin) admin.value = sent.admin.trim().replace(/^@+/, '')
    await refresh({ dedupe: 'cancel' })
  }, { title: 'Настройки бота сохранены', color: 'success' }, { title: 'Ошибка' })
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
        v-model="token" class="in mono" :type="showToken ? 'text' : 'password'" autocomplete="off" data-1p-ignore data-lpignore="true"
        data-bwignore :placeholder="data?.bot_token_masked || ''"><button type="button" class="xb eye" aria-label="Показать" :aria-pressed="showToken" @click="showToken = !showToken"><svg class="i" viewBox="0 0 24 24"><path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6S2 12 2 12z" /><circle cx="12" cy="12" r="2.6" /></svg></button></label>
      <label class="fld"><span>Chat ID</span><input
        v-model="chatId" class="in mono" autocomplete="off" data-1p-ignore data-lpignore="true" data-bwignore inputmode="numeric"></label>
      <label class="fld"><span>Никнейм админа бота</span><input
        v-model="admin" class="in" autocomplete="off" data-1p-ignore data-lpignore="true" data-bwignore placeholder="@ник"></label>
      <div class="fld">
        <span />
        <button type="button" class="pill w" :disabled="!Object.keys(body).length || busy.has('save')" @click="save">Сохранить</button>
      </div>
    </div>
  </div>
</template>
