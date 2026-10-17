<script setup lang="ts">
import type { NodeMetric } from '~/composables/useMonitoring'
import { exitName } from '~/utils/overview-nodes'

// Окно узла: модалка на десктопе, нижняя шторка на телефоне. Содержимое общее,
// подтверждение выключения встаёт в том же окне вместо подробностей.
const props = defineProps<{
  node: NodeMetric | null
  nodes: NodeMetric[]
  data: number[]
  setExitDisabled: (tag: string, disabled: boolean) => Promise<void>
}>()
const open = defineModel<boolean>('open', { default: false })
const mobile = useMobile()

const asking = ref(false)
// узел держим, пока окно закрывается: иначе содержимое пропадёт до конца анимации
const n = ref<NodeMetric | null>(null)
watch(() => props.node, (v) => { if (v) n.value = v }, { immediate: true })
watch(open, (v) => { if (v) asking.value = false })
const name = computed(() => n.value ? exitName(n.value.tag) : '')
const title = computed(() => asking.value ? `Выключить ${name.value}?` : name.value)
</script>

<template>
  <BottomSheet v-if="mobile" :open="open" :label="title" @close="open = false">
    <div class="ov-sh">
      <OverviewNodeBody
        v-model:asking="asking" :node="n" :nodes="nodes" :data="data" :open="open"
        :set-exit-disabled="setExitDisabled" @close="open = false"
      />
    </div>
  </BottomSheet>
  <UModal
    v-else
    v-model:open="open" :title="title" :close="false"
    :ui="{ overlay: 'ov-ovl', content: asking ? 'card ov-modal' : 'card ov-modal wide' }"
  >
    <template #content>
      <OverviewNodeBody
        v-model:asking="asking" :node="n" :nodes="nodes" :data="data" :open="open"
        :set-exit-disabled="setExitDisabled" @close="open = false"
      />
    </template>
  </UModal>
</template>
