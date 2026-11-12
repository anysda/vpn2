<script setup lang="ts">
const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ created: [id: number] }>()
const { canSend, reason: tgReason } = useTelegramStatus()
const toast = useToast()

const form = reactive({ name: '', expires: '', limit: 3 as number | string, unlimited: false, filter: true, bot: false })
const busy = ref(false)
watch(open, now => now && Object.assign(form, { name: '', expires: '', limit: 3, unlimited: false, filter: true, bot: false }))

async function submit() {
  const name = form.name.trim()
  if (!name) return toast.add({ title: 'Укажите имя', color: 'error' })
  const expiresAt = form.expires.trim() ? parseRuDateUtc(form.expires) : null
  if (form.expires.trim() && !expiresAt) return toast.add({ title: 'Дата — в формате ДД.ММ.ГГГГ', color: 'error' })
  busy.value = true
  try {
    const deviceLimit = form.unlimited ? null : Math.max(1, Math.floor(Number(form.limit) || 1))
    const client = await createClient({ name, filterTraffic: form.filter, expiresAt, deviceLimit })
    open.value = false
    toast.add(doneToast(`Клиент «${name}» создан`, client.warning))
    emit('created', client.id)
    if (form.bot && canSend.value) {
      await sendBotAccess(client.id).then(
        () => toast.add({ title: 'Приглашение отправлено в Telegram', color: 'success' }),
        () => toast.add({ title: 'Клиент создан, но пароль не отправлен в Telegram', color: 'warning' }))
    }
  }
  catch (e) {
    toast.add({ title: errText(e), color: 'error' })
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <Dlg v-model:open="open" title="Новый клиент" ok="Создать" :busy="busy" @ok="submit">
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
      <button type="button" role="switch" class="tog" :class="{ on: form.filter }" :aria-checked="form.filter" aria-label="Фильтровать трафик (AdGuard)" @click="form.filter = !form.filter">
        <i />
      </button>
    </div>
    <div class="crw">
      <span>Отправить доступ к боту</span>
      <span :title="tgReason || undefined">
        <button
          type="button" role="switch" class="tog" :class="{ on: form.bot && canSend }" :aria-checked="form.bot && canSend" aria-label="Отправить доступ к боту"
          :disabled="!canSend" @click="form.bot = !form.bot">
          <i />
        </button>
      </span>
    </div>
  </Dlg>
</template>
