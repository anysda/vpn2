<script setup lang="ts">
import type { Client } from '~/composables/useClients'
import { useClients } from '~/composables/useClients'

const props = defineProps<{ client: Client }>()
const open = defineModel<boolean>('open', { default: false })

const { update } = useClients()
const toast = useToast()

const name = ref(props.client.name)
const expiresAt = ref(props.client.expiresAt ? props.client.expiresAt.slice(0, 10) : '')

watch(open, (now) => {
  if (now) {
    name.value = props.client.name
    expiresAt.value = props.client.expiresAt ? props.client.expiresAt.slice(0, 10) : ''
  }
})

const loading = ref(false)

async function submit() {
  if (!name.value.trim()) return
  loading.value = true
  try {
    await update(props.client.id, {
      name: name.value.trim(),
      expiresAt: expiresAt.value ? new Date(expiresAt.value).toISOString() : null,
    })
    toast.add({ title: 'Сохранено', color: 'success' })
    open.value = false
  }
  catch (e) {
    toast.add({ title: 'Ошибка сохранения', color: 'error', description: (e as Error).message })
  }
  finally {
    loading.value = false
  }
}
</script>

<template>
  <UModal v-model:open="open" title="Редактировать клиента" :ui="{ content: 'max-w-md' }">
    <template #body>
      <form class="space-y-3" @submit.prevent="submit">
        <UFormField label="Имя" required>
          <UInput v-model="name" class="w-full" autofocus />
        </UFormField>
        <UFormField label="Истекает (пусто = бессрочный)">
          <UInput v-model="expiresAt" type="date" class="w-full" />
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
        Сохранить
      </UButton>
    </template>
  </UModal>
</template>
