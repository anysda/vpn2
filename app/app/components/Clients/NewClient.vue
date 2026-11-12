<script setup lang="ts">
import { createClient } from '~/composables/useClients'
const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ created: [id: number] }>()
const { canSend, reason: tgReason } = useTelegramStatus()
const { toast, busy, act } = useAct()

const BLANK = { name: '', expires: '', limit: 3 as number | string, unlimited: false, filter: true, bot: false }
const form = reactive({ ...BLANK })
watch(open, now => now && Object.assign(form, BLANK))

function submit() {
  const name = form.name.trim()
  if (!name) return toast.add({ title: 'Укажите имя', color: 'error' })
  const expiresAt = form.expires.trim() ? parseRuDateUtc(form.expires) : null
  if (form.expires.trim() && !expiresAt) return toast.add({ title: 'Дата — в формате ДД.ММ.ГГГГ', color: 'error' })
  const deviceLimit = form.unlimited ? null : Math.max(1, Math.floor(Number(form.limit) || 1))
  return act('create', async () => {
    const client = await createClient({ name, filterTraffic: form.filter, expiresAt, deviceLimit })
    open.value = false
    toast.add(doneToast(`Клиент «${name}» создан`, client.warning))
    emit('created', client.id)
    if (!form.bot || !canSend.value) return
    await sendBotAccess(client.id).then(
      () => toast.add({ title: 'Приглашение отправлено в Telegram', color: 'success' }),
      () => toast.add({ title: 'Клиент создан, но пароль не отправлен в Telegram', color: 'warning' }))
  })
}
</script>

<template>
  <PanelDialog v-model:open="open" title="Новый клиент" ok="Создать" :busy="busy.has('create')" @ok="submit">
    <label class="fl2"><span>Имя</span><input v-model="form.name" class="in" placeholder="Иван Иванов" @keydown.enter="submit"></label>
    <label class="fl2">
      <span>Срок действия</span>
      <input
        v-model="form.expires" class="in" placeholder="ДД.ММ.ГГГГ" inputmode="numeric" maxlength="10"
        @input="form.expires = maskRuDate(form.expires)" @keydown.enter="submit">
      <small class="sub2">пусто = бессрочно</small>
    </label>
    <div class="fl2">
      <span>Лимит девайсов</span>
      <div class="flex items-center gap-2">
        <input v-model="form.limit" class="in w-[120px]!" type="number" min="1" aria-label="Лимит девайсов" :disabled="form.unlimited">
        <button
          type="button" class="pill" :class="{ on: form.unlimited }" title="Безлимитно" aria-label="Безлимитно" :aria-pressed="form.unlimited"
          @click="form.unlimited = !form.unlimited">
          ∞
        </button>
      </div>
    </div>
    <div class="crw mt-4">
      <span>Фильтровать трафик (AdGuard)</span>
      <PanelSwitch v-model="form.filter" label="Фильтровать трафик (AdGuard)" />
    </div>
    <div class="crw">
      <span>Отправить доступ к боту</span>
      <span :title="tgReason || undefined">
        <PanelSwitch :model-value="form.bot && canSend" label="Отправить доступ к боту" :disabled="!canSend" @update:model-value="form.bot = !form.bot" />
      </span>
    </div>
  </PanelDialog>
</template>
