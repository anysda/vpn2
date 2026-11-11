<script setup lang="ts">
import { useClientDevices } from '~/composables/useClients'

const props = defineProps<{ clientId: number }>()
const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ added: [] }>()

const { addDevice } = useClientDevices()
const toast = useToast()

const name = ref('')
const busy = ref(false)

watch(open, (now) => {
  if (now) name.value = ''
})

async function submit() {
  const n = name.value.trim()
  if (!n || busy.value) return
  busy.value = true
  try {
    const res = await addDevice(props.clientId, n)
    open.value = false
    toast.add(doneToast(`Девайс «${n}» добавлен`, res.warning))
    emit('added')
  }
  catch (e) {
    toast.add({ title: errText(e, 'Не удалось добавить девайс'), color: 'error' })
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <PanelModal
    v-model:open="open"
    class="clm"
    title="Новый девайс"
    closable
    ok="Добавить"
    :ok-disabled="!name.trim() || busy"
    @ok="submit">
    <label class="fl2">
      <span>Название девайса</span>
      <input v-model="name" class="in" placeholder="напр. iPhone" @keydown.enter="submit">
    </label>
  </PanelModal>
</template>
