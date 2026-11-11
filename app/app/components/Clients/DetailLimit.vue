<script setup lang="ts">
import type { ClientDetail } from '~/composables/useClients'
import { useClients } from '~/composables/useClients'
import { errTitle } from '~/utils/clients-view'

const props = defineProps<{ detail: ClientDetail }>()
const emit = defineEmits<{ saved: [id: number, limit: number | null] }>()

const { update } = useClients()
const toast = useToast()

const limDraft = ref('')
const minLimit = computed(() => Math.max(1, props.detail.devices.length))

const limText = (d: ClientDetail) => d.deviceLimit === null ? '' : String(d.deviceLimit)

// Карточка перезагружается после сохранения срока или девайса, когда человек
// уже мог начать править лимит: черновик берёт значение сервера, только если в
// нём то, что было показано до перезагрузки. Другой клиент — поле заново.
watch(() => props.detail, (d, prev) => {
  const same = prev?.id === d.id ? prev : null
  if (!same || limDraft.value === limText(same)) limDraft.value = limText(d)
}, { immediate: true })

async function saveLimit(next: number | null, withToast: boolean) {
  const d = props.detail
  if (next === d.deviceLimit) {
    limDraft.value = next === null ? '' : String(next)
    return
  }
  limDraft.value = next === null ? '' : String(next)
  try {
    const res = await update(d.id, { deviceLimit: next })
    emit('saved', d.id, next)
    if (withToast || res.warning) toast.add(doneToast('Лимит девайсов сохранён', res.warning))
  }
  catch (e) {
    toast.add({ title: errTitle(e, 'Ошибка сохранения лимита'), color: 'error' })
    limDraft.value = d.deviceLimit === null ? '' : String(d.deviceLimit)
  }
}

function onLimitChange() {
  const v = limDraft.value.trim()
  if (!v) return saveLimit(null, true)
  const n = Number.parseInt(v, 10)
  if (!(n >= minLimit.value)) {
    toast.add({ title: `Лимит — не меньше ${minLimit.value}`, color: 'error' })
    limDraft.value = limText(props.detail)
    return
  }
  return saveLimit(n, true)
}

function limitMinus() {
  const lim = props.detail.deviceLimit
  if (lim === null) return saveLimit(minLimit.value, false)
  if (lim > minLimit.value) return saveLimit(lim - 1, false)
}

function limitPlus() {
  const lim = props.detail.deviceLimit
  return saveLimit(lim === null ? minLimit.value : lim + 1, false)
}

function limitInfinity() {
  const d = props.detail
  return saveLimit(d.deviceLimit === null ? Math.max(d.devices.length, 3) : null, false)
}
</script>

<template>
  <div class="crw">
    <span>Лимит девайсов<template v-if="detail.deviceLimit === null"> · <b>лимит снят (∞)</b></template></span>
    <div class="stp">
      <button type="button" aria-label="Меньше" :disabled="detail.deviceLimit !== null && detail.deviceLimit <= minLimit" @click="limitMinus">
        −
      </button>
      <input
        v-model="limDraft" class="in" placeholder="∞" inputmode="numeric" aria-label="Лимит девайсов" @change="onLimitChange"
        @keydown.enter.prevent="($event.target as HTMLInputElement).blur()">
      <button type="button" aria-label="Больше" @click="limitPlus">
        +
      </button>
    </div>
    <button
      type="button" class="pill" :class="{ on: detail.deviceLimit === null }" title="Безлимитно" aria-label="Безлимитно"
      :aria-pressed="detail.deviceLimit === null" @click="limitInfinity">
      ∞
    </button>
  </div>
</template>
