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
    await addDevice(props.clientId, n)
    open.value = false
    toast.add({ title: `Девайс «${n}» добавлен`, color: 'success' })
    emit('added')
  }
  catch (e) {
    const err = e as { data?: { message?: string }, statusMessage?: string }
    toast.add({ title: err.data?.message ?? err.statusMessage ?? 'Не удалось добавить девайс', color: 'error' })
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <ClientsModal v-model:open="open" title="Новый девайс" closable>
    <label class="fl2"><span>Название девайса</span><input
      v-model="name"
      class="in"
      placeholder="напр. iPhone"
      @keydown.enter="submit"
    ></label>
    <div class="mf">
      <button type="button" class="pill" @click="open = false">
        Отмена
      </button>
      <button type="button" class="pill w" :disabled="!name.trim() || busy" @click="submit">
        Добавить
      </button>
    </div>
  </ClientsModal>
</template>
