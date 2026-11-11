<script setup lang="ts">
import { parseRuDateUtc } from '~/utils/format'
import { maskRuDate } from '~/utils/ru-date-mask'

const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ created: [id: number] }>()

const { canSend: canSendTg, reason: tgReason } = useTelegramStatus()
const toast = useToast()

const DEFAULT_DEVICE_LIMIT = 3

const name = ref('')
const expires = ref('')
const limit = ref<number | string>(DEFAULT_DEVICE_LIMIT)
const unlimited = ref(false)
const filterTraffic = ref(true)
const sendBot = ref(false)
const busy = ref(false)

watch(open, (now) => {
  if (!now) return
  name.value = ''
  expires.value = ''
  limit.value = DEFAULT_DEVICE_LIMIT
  unlimited.value = false
  filterTraffic.value = true
  sendBot.value = false
})

function onExpiresInput(e: Event) {
  const el = e.target as HTMLInputElement
  el.value = maskRuDate(el.value)
  expires.value = el.value
}

async function submit() {
  const n = name.value.trim()
  if (!n) {
    toast.add({ title: 'Укажите имя', color: 'error' })
    return
  }
  let expiresAt: string | null = null
  if (expires.value.trim()) {
    expiresAt = parseRuDateUtc(expires.value)
    if (!expiresAt) {
      toast.add({ title: 'Дата — в формате ДД.ММ.ГГГГ', color: 'error' })
      return
    }
  }
  busy.value = true
  try {
    const client = await createClient({
      name: n,
      filterTraffic: filterTraffic.value,
      expiresAt,
      deviceLimit: unlimited.value ? null : Math.max(1, Math.floor(Number(limit.value) || 1)),
    })
    open.value = false
    toast.add(doneToast(`Клиент «${n}» создан`, client.warning))
    emit('created', client.id)
    if (sendBot.value && canSendTg.value) {
      try {
        await sendBotAccess(client.id)
        toast.add({ title: 'Приглашение отправлено в Telegram', color: 'success' })
      }
      catch {
        toast.add({ title: 'Клиент создан, но пароль не отправлен в Telegram', color: 'warning' })
      }
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
  <PanelModal v-model:open="open" class="clm" title="Новый клиент" closable ok="Создать" :ok-disabled="busy" @ok="submit">
    <label class="fl2">
      <span>Имя</span>
      <input v-model="name" class="in" placeholder="Иван Иванов" @keydown.enter="submit">
    </label>
    <label class="fl2">
      <span>Срок действия</span>
      <input
        :value="expires"
        class="in"
        placeholder="ДД.ММ.ГГГГ"
        inputmode="numeric"
        maxlength="10"
        @input="onExpiresInput"
        @keydown.enter="submit">
      <small class="sub2" style="margin:0">пусто = бессрочно</small>
    </label>
    <div class="fl2">
      <span>Лимит девайсов</span>
      <div class="arow" style="margin:0">
        <input v-model="limit" class="in" type="number" min="1" style="width:120px" aria-label="Лимит девайсов" :disabled="unlimited">
        <button
          type="button"
          class="pill"
          :class="{ on: unlimited }"
          title="Безлимитно"
          aria-label="Безлимитно"
          :aria-pressed="unlimited"
          @click="unlimited = !unlimited">
          ∞
        </button>
      </div>
    </div>
    <div class="crw" style="margin-top:18px">
      <span>Фильтровать трафик (AdGuard)</span>
      <ToggleSwitch :on="filterTraffic" label="Фильтровать трафик (AdGuard)" @click="filterTraffic = !filterTraffic" />
    </div>
    <div class="crw">
      <span>Отправить доступ к боту</span>
      <span :title="canSendTg ? undefined : tgReason">
        <ToggleSwitch :on="sendBot && canSendTg" label="Отправить доступ к боту" :disabled="!canSendTg" @click="sendBot = !sendBot" />
      </span>
    </div>
  </PanelModal>
</template>
