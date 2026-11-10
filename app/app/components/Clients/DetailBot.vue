<script setup lang="ts">
import type { ClientDetail } from '~/composables/useClients'
import { useClients } from '~/composables/useClients'
import { useTelegramStatus } from '~/composables/useTelegramStatus'
import { errTitle } from '~/utils/clients-view'
import type { Confirm } from './Ask.vue'

const props = defineProps<{ detail: ClientDetail, confirm: Confirm }>()
const emit = defineEmits<{ unlinked: [id: number] }>()

const { sendPasswordToTg, unlink } = useClients()
const { canSend: canSendTg, reason: tgReason } = useTelegramStatus()
const toast = useToast()

const sendingPwd = ref(false)
async function sendPassword() {
  sendingPwd.value = true
  try {
    await sendPasswordToTg(props.detail.id)
    toast.add({ title: 'Приглашение отправлено в Telegram', color: 'success' })
  }
  catch (e) {
    toast.add({ title: errTitle(e, 'Ошибка отправки'), color: 'error' })
  }
  finally {
    sendingPwd.value = false
  }
}

function askUnlink() {
  const d = props.detail
  props.confirm({
    title: 'Отозвать доступ к боту?',
    pre: 'Отвязать Telegram клиента ',
    name: d.name,
    post: '? Он потеряет доступ к самообслуживанию в боте, а старая ссылка-приглашение перестанет работать. Конфиги VPN продолжат работать.',
    act: 'Отозвать',
    fail: 'Ошибка',
    run: async () => {
      const res = await unlink(d.id)
      emit('unlinked', d.id)
      toast.add(doneToast('Доступ к боту отозван', res.warning))
    },
  })
}
</script>

<template>
  <div class="crw bt">
    <span>Бот · <b>{{ detail.tgLinked ? (detail.tgUsername ? `@${detail.tgUsername}` : 'привязан') : 'не привязан' }}</b></span>
    <div class="bta">
      <span :title="canSendTg ? undefined : tgReason">
        <button
          type="button"
          class="pill w"
          :disabled="!canSendTg || sendingPwd"
          @click="sendPassword"
        >Отправить доступ к боту</button>
      </span>
      <button
        v-if="detail.tgLinked"
        type="button"
        class="pill red"
        @click="askUnlink"
      >
        Отозвать доступ к боту
      </button>
    </div>
  </div>
</template>
