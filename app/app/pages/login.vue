<script setup lang="ts">
definePageMeta({ layout: false })

const { loggedIn, fetch: refreshSession } = useUserSession()

if (loggedIn.value) {
  await navigateTo('/')
}

const state = reactive({
  username: '',
  password: '',
  totpCode: '',
})

const needsTotp = ref(false)
const error = ref<string | null>(null)
const loading = ref(false)

async function submit() {
  error.value = null
  loading.value = true
  try {
    const res = await $fetch<{ ok?: boolean, needsTotp?: boolean }>('/api/auth/login', {
      method: 'POST',
      body: {
        username: state.username,
        password: state.password,
        totpCode: needsTotp.value ? state.totpCode : undefined,
      },
    })
    if (res.needsTotp) {
      needsTotp.value = true
      return
    }
    await refreshSession()
    await navigateTo('/')
  }
  catch (e) {
    const err = e as { statusMessage?: string, message?: string }
    error.value = err.statusMessage ?? err.message ?? 'Ошибка'
  }
  finally {
    loading.value = false
  }
}

function reset() {
  needsTotp.value = false
  state.totpCode = ''
  error.value = null
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-(--ui-page) p-4">
    <UCard class="w-full max-w-sm">
      <template #header>
        <div class="text-center font-semibold text-lg">
          anysda-vpn2
        </div>
      </template>

      <form class="space-y-4" @submit.prevent="submit">
        <UFormField label="Логин" required>
          <UInput
            v-model="state.username"
            :disabled="needsTotp || loading"
            placeholder="admin"
            autocomplete="username"
            class="w-full"
          />
        </UFormField>
        <UFormField label="Пароль" required>
          <UInput
            v-model="state.password"
            type="password"
            :disabled="needsTotp || loading"
            autocomplete="current-password"
            class="w-full"
          />
        </UFormField>

        <UFormField v-if="needsTotp" label="Код из аутентификатора" required>
          <UInput
            v-model="state.totpCode"
            placeholder="123 456"
            autocomplete="one-time-code"
            inputmode="numeric"
            class="w-full"
            autofocus
          />
        </UFormField>

        <UAlert
          v-if="error"
          color="error"
          variant="soft"
          :title="error"
          icon="i-lucide-circle-alert"
        />

        <div class="flex gap-2">
          <UButton
            v-if="needsTotp"
            color="neutral"
            variant="outline"
            :disabled="loading"
            @click="reset"
          >
            Назад
          </UButton>
          <UButton
            type="submit"
            block
            :loading="loading"
          >
            {{ needsTotp ? 'Подтвердить' : 'Войти' }}
          </UButton>
        </div>
      </form>
    </UCard>
  </div>
</template>
