<script setup lang="ts">
import { errTitle } from '~/utils/clients-view'

/** Вопрос перед необратимым действием; `fail` - заголовок тоста, если у ошибки нет своего текста. */
export interface Ask { title: string, pre: string, name: string, post: string, act: string, danger?: boolean, fail: string, run: () => Promise<void> }
export type Confirm = (a: Ask) => void

const toast = useToast()
const ask = ref<Ask | null>(null)
const askOpen = ref(false)
const askBusy = ref(false)

function confirm(a: Ask) {
  ask.value = a
  askOpen.value = true
}

async function runAsk() {
  const a = ask.value
  if (!a || askBusy.value) return
  askBusy.value = true
  try {
    await a.run()
    askOpen.value = false
  }
  catch (e) {
    // Окно остаётся открытым.
    toast.add({ title: errTitle(e, a.fail), color: 'error' })
  }
  finally {
    askBusy.value = false
  }
}

defineExpose({ confirm })
</script>

<template>
  <ClientsModal v-model:open="askOpen" :title="ask?.title ?? ''">
    <p class="mt">
      {{ ask?.pre }}<b>«{{ ask?.name }}»</b>{{ ask?.post }}
    </p>
    <div class="mf">
      <button type="button" class="pill" @click="askOpen = false">
        Отмена
      </button>
      <button
        type="button"
        class="pill"
        :class="ask?.danger ? 'redw' : 'w'"
        :disabled="askBusy"
        @click="runAsk"
      >
        {{ ask?.act }}
      </button>
    </div>
  </ClientsModal>
</template>
