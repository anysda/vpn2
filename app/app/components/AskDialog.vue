<script setup lang="ts">
// Окно вопроса useAsk(): при отказе сервера остаётся открытым, причина - тостом.
const ask = useAsk()
const toast = useToast()
const busy = ref(false)
const open = computed({ get: () => !!ask.value, set: (v) => { if (!v) ask.value = null } })

async function run() {
  const a = ask.value
  if (!a || busy.value) return
  busy.value = true
  try {
    await a.run()
    ask.value = null
  }
  catch (e) {
    toast.add({ title: errText(e, a.fail), color: 'error' })
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <Dlg v-model:open="open" :title="ask?.title ?? ''" :ok="ask?.ok" :danger="ask?.danger" :busy="busy" @ok="run">
    <p class="mt">
      {{ ask?.text }}
    </p>
  </Dlg>
</template>
