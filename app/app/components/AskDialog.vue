<script setup lang="ts">
// Окно вопроса useAsk(): при отказе сервера остаётся открытым, причина - тостом.
const ask = useAsk()
const { busy, act } = useAct()
const open = computed({ get: () => !!ask.value, set: (v) => { if (!v) ask.value = null } })

async function run() {
  const a = ask.value
  if (a && !busy.value.has('ask') && await act('ask', a.run, undefined, a.fail)) ask.value = null
}
</script>

<template>
  <PanelDialog v-model:open="open" :title="ask?.title ?? ''" :ok="ask?.ok" :danger="ask?.danger" :busy="busy.has('ask')" @ok="run">
    <p class="mt">
      {{ ask?.text }}
    </p>
  </PanelDialog>
</template>
