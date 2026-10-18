<script setup lang="ts">
import { accessProtocols, accessStatus, protocolNames } from '#shared/access-operation'
import type { AccessOperation } from '#shared/access-operation'

const props = defineProps<{ operation: AccessOperation | null, deleting?: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const busy = ref(false)
const toast = useToast()
const title = computed(() => props.operation ? accessStatus(props.operation) : 'Состояние не подтверждено')
async function retry() {
  if (!props.operation || busy.value) return
  busy.value = true
  try {
    await $fetch(`/api/access-operations/${props.operation.id}/retry`, { method: 'POST' })
    emit('changed')
  }
  catch { toast.add({ title: 'Не удалось повторить операцию', color: 'error' }) }
  finally { busy.value = false }
}
</script>

<template>
  <div class="crw" role="status" aria-live="polite">
    <span>
      <small v-if="operation?.deviceId">Устройство #{{ operation.deviceId }} · </small>
      <b v-if="deleting">Удаляется · </b><b>{{ title }}</b>
      <template v-if="operation">
        <span v-for="p in accessProtocols" :key="p">
          <br>
          {{ protocolNames[p] }} · {{ operation.protocols[p].state === 'not_required' ? 'Не требуется' : operation.protocols[p].state === 'confirmed' && title !== 'Состояние не подтверждено' ? 'Подтверждено' : operation.protocols[p].error || 'Состояние не подтверждено' }}
        </span>
      </template>
    </span>
    <button v-if="operation && operation.status !== 'superseded' && (operation.status !== 'confirmed' || title === 'Состояние не подтверждено')" type="button" class="pill w" :disabled="busy" @click="retry">Повторить</button>
  </div>
</template>
