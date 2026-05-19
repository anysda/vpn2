<script setup lang="ts">
import { useClients } from '~/composables/useClients'

const { create } = useClients()
const toast = useToast()

const open = ref(false)
const name = ref('')
const expiresAt = ref<string>('')
const loading = ref(false)

async function submit() {
  if (!name.value.trim()) return
  loading.value = true
  try {
    await create({
      name: name.value.trim(),
      expiresAt: expiresAt.value ? new Date(expiresAt.value).toISOString() : null,
    })
    toast.add({ title: `Клиент «${name.value}» создан`, color: 'success' })
    name.value = ''
    expiresAt.value = ''
    open.value = false
  }
  catch (e) {
    const err = e as { statusMessage?: string, message?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка', color: 'error' })
  }
  finally {
    loading.value = false
  }
}

function reset() {
  name.value = ''
  expiresAt.value = ''
  open.value = false
}
</script>

<template>
  <div>
    <UButton
      v-if="!open"
      block
      icon="i-lucide-plus"
      color="primary"
      variant="soft"
      @click="open = true"
    >
      Новый клиент
    </UButton>

    <form
      v-else
      class="space-y-2 p-2 border border-(--ui-border) rounded-md"
      @submit.prevent="submit"
    >
      <UFormField label="Имя">
        <UInput
          v-model="name"
          placeholder="напр. anysda-phone"
          autofocus
          class="w-full"
        />
      </UFormField>
      <UFormField label="Истекает (опционально)">
        <UInput
          v-model="expiresAt"
          type="date"
          class="w-full"
        />
      </UFormField>
      <div class="flex gap-2">
        <UButton
          color="neutral"
          variant="soft"
          :disabled="loading"
          @click="reset"
        >
          Отмена
        </UButton>
        <UButton
          type="submit"
          color="primary"
          :loading="loading"
          :disabled="!name.trim()"
        >
          Создать
        </UButton>
      </div>
    </form>
  </div>
</template>
