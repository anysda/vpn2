<script setup lang="ts">
/** Вопрос перед необратимым действием; `fail` - заголовок тоста, если у ошибки нет своего текста. */
export interface Ask { title: string, pre: string, name: string, post: string, act: string, danger?: boolean, fail: string, run: () => Promise<void> }
export type Confirm = (a: Ask) => void

const toast = useToast()
const ask = ref<Ask>()
const open = ref(false)
const busy = ref(false)

function confirm(a: Ask) {
  ask.value = a
  open.value = true
}

async function run() {
  const a = ask.value
  if (!a || busy.value) return
  busy.value = true
  try {
    await a.run()
    open.value = false
  }
  catch (e) {
    // Окно остаётся открытым.
    toast.add({ title: errText(e, a.fail), color: 'error' })
  }
  finally {
    busy.value = false
  }
}

defineExpose({ confirm })
</script>

<template>
  <PanelModal
    v-model:open="open"
    class="clm"
    :title="ask?.title ?? ''"
    :ok="ask?.act"
    :ok-class="ask?.danger ? 'redw' : 'w'"
    :ok-disabled="busy"
    @ok="run">
    <p class="mt">
      {{ ask?.pre }}<b>«{{ ask?.name }}»</b>{{ ask?.post }}
    </p>
  </PanelModal>
</template>
