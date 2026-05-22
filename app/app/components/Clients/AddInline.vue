<script setup lang="ts">
import { useClients } from '~/composables/useClients'

const { create } = useClients()
const toast = useToast()

const open = ref(false)
const name = ref('')
const filterTraffic = ref(true)
const loading = ref(false)

async function submit() {
  if (!name.value.trim()) return
  loading.value = true
  try {
    await create({
      name: name.value.trim(),
      filterTraffic: filterTraffic.value,
    })
    toast.add({ title: `Клиент «${name.value.trim()}» создан`, color: 'success' })
    reset()
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
  filterTraffic.value = true
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
          placeholder="напр. Dima"
          autofocus
          class="w-full"
        />
      </UFormField>
      <div class="flex items-center justify-between pt-1">
        <label class="text-sm flex items-center gap-2 cursor-pointer">
          <UIcon name="i-simple-icons-adguard" class="size-4 text-[#67b279]" />
          Фильтровать трафик (AdGuard)
        </label>
        <USwitch v-model="filterTraffic" />
      </div>
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
