<script setup lang="ts">
import type { ClientDetail } from '~/composables/useClients'
import { useClients } from '~/composables/useClients'
import { errTitle } from '~/utils/clients-view'
import type { AccessOperation } from '#shared/access-operation'
import type { Confirm } from './Ask.vue'

const props = defineProps<{ detail: ClientDetail, confirm: Confirm, reload: () => Promise<void> }>()
const emit = defineEmits<{
  deleting: [id: number, accepted: Promise<{ operation: AccessOperation }>]
  accepted: [id: number, operation: AccessOperation]
}>()

const { update, remove, reissueAll } = useClients()
const toast = useToast()

function askReissueAll() {
  const d = props.detail
  props.confirm({
    title: 'Перевыпустить все ключи?',
    pre: 'Перевыпустить ключи всех девайсов клиента ',
    name: d.name,
    post: '? После применения старые конфиги перестанут работать — нужно будет раздать новые.',
    act: 'Перевыпустить',
    fail: 'Ошибка перевыпуска',
    run: async () => {
      await reissueAll(d.id)
      toast.add({ title: 'Перевыпуск выполняется', color: 'info' })
      await props.reload()
    },
  })
}

function askRemoveClient() {
  const d = props.detail
  props.confirm({
    title: 'Удалить клиента?',
    pre: 'Удалить клиента ',
    name: d.name,
    post: ' со всеми девайсами? Действие необратимо; доступ прекратится после подтверждения отключения.',
    act: 'Удалить',
    danger: true,
    fail: 'Ошибка удаления',
    run: async () => {
      const deleting = remove(d.id)
      emit('deleting', d.id, deleting)
      const result = await deleting
      emit('accepted', d.id, result.operation)
      toast.add({ title: 'Отключение выполняется', color: 'info' })
      await props.reload()
    },
  })
}

const freezing = ref(false)
async function toggleFreeze() {
  const d = props.detail
  const next = !d.frozenManual
  freezing.value = true
  try {
    await update(d.id, { frozenManual: next })
    toast.add({ title: next ? 'Отключение выполняется' : 'Включение выполняется', color: 'info' })
    await props.reload()
  }
  catch (e) {
    toast.add({ title: errTitle(e, 'Ошибка'), color: 'error' })
  }
  finally {
    freezing.value = false
  }
}
</script>

<template>
  <div class="acts">
    <button type="button" class="pill" @click="askReissueAll">
      Перевыпустить все ключи
    </button>
    <button
      type="button"
      class="pill"
      :disabled="freezing"
      @click="toggleFreeze"
    >
      {{ detail.frozenManual ? 'Разморозить' : 'Заморозить' }}
    </button>
    <button type="button" class="pill red" @click="askRemoveClient">
      Удалить клиента
    </button>
  </div>
</template>
