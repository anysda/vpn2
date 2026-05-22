<script setup lang="ts">
import { useClientDevices } from '~/composables/useClients'

const props = defineProps<{ clientId: number }>()
const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ added: [] }>()

const { addDevice } = useClientDevices()
const toast = useToast()

const name = ref('')
const loading = ref(false)

watch(open, (now) => {
  if (now) name.value = ''
})

async function submit() {
  if (!name.value.trim()) return
  loading.value = true
  try {
    await addDevice(props.clientId, name.value.trim())
    toast.add({ title: `Девайс «${name.value.trim()}» добавлен`, color: 'success' })
    open.value = false
    emit('added')
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Не удалось добавить девайс', color: 'error' })
  }
  finally {
    loading.value = false
  }
}
</script>

<template>
  <UModal v-model:open="open" title="Новый девайс" :ui="{ content: 'max-w-sm' }">
    <template #body>
      <form class="space-y-3" @submit.prevent="submit">
        <UFormField label="Название девайса" required>
          <UInput
            v-model="name"
            placeholder="напр. iPhone"
            autofocus
            class="w-full"
          />
        </UFormField>
      </form>
    </template>
    <template #footer>
      <UButton color="neutral" variant="soft" @click="open = false">
        Отмена
      </UButton>
      <UButton
        color="primary"
        :loading="loading"
        :disabled="!name.trim()"
        @click="submit"
      >
        Добавить
      </UButton>
    </template>
  </UModal>
</template>
