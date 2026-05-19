<script setup lang="ts">
import type { Outbound, Route } from '~/composables/useRoutes'
import { flagFor, rttColor, useRoutes } from '~/composables/useRoutes'

const { rules, outbounds, create, patch, remove } = useRoutes()
const toast = useToast()

const newValue = ref('')
const newOutbound = ref<string>('')
const adding = ref(false)

// Default newOutbound to "direct-ru" once we know the proxy list
watch(outbounds, (list) => {
  if (!newOutbound.value && list.length > 0) {
    newOutbound.value = list.find(o => o.isDirectRu)?.name ?? list[0]!.name
  }
}, { immediate: true })

async function add() {
  if (!newValue.value.trim() || !newOutbound.value) return
  adding.value = true
  try {
    await create(newValue.value.trim(), newOutbound.value)
    toast.add({ title: 'Правило добавлено', color: 'success' })
    newValue.value = ''
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Ошибка', color: 'error' })
  }
  finally {
    adding.value = false
  }
}

const directOutbounds = computed(() => outbounds.value.filter(o => o.isDirectRu || (!o.isWarp && o.name.startsWith('hy2-'))))
const warpOutbounds = computed(() => outbounds.value.filter(o => o.isWarp))

function rulesFor(outboundName: string): Route[] {
  return rules.value.filter(r => r.outbound === outboundName)
}

const moving = ref<{ id: number, value: string } | null>(null)

async function dropTo(targetOutbound: string) {
  if (!moving.value) return
  const m = moving.value
  moving.value = null
  if (rules.value.find(r => r.id === m.id)?.outbound === targetOutbound) return
  try {
    await patch(m.id, targetOutbound)
    toast.add({ title: `«${m.value}» → ${targetOutbound}`, color: 'success' })
  }
  catch (e) {
    toast.add({ title: 'Ошибка перемещения', color: 'error', description: (e as Error).message })
  }
}

async function deleteRule(rule: Route) {
  try {
    await remove(rule.id)
    toast.add({ title: `«${rule.value}» удалено`, color: 'success' })
  }
  catch (e) {
    toast.add({ title: 'Ошибка', color: 'error', description: (e as Error).message })
  }
}
</script>

<template>
  <UCard>
    <template #header>
      <div class="flex items-center justify-between">
        <div class="font-semibold">
          Маршрутизация
        </div>
        <span class="text-xs text-(--ui-text-muted)">
          {{ rules.length }} {{ rules.length === 1 ? 'правило' : 'правил' }}
        </span>
      </div>
    </template>

    <div class="space-y-4">
      <!-- Add form -->
      <form
        class="flex flex-wrap gap-2 items-end"
        @submit.prevent="add"
      >
        <div class="flex-1 min-w-[200px]">
          <UInput
            v-model="newValue"
            placeholder="домен (netflix.com, *.openai.com) или CIDR (8.8.8.8/32)"
            class="w-full"
          />
        </div>
        <USelect
          v-model="newOutbound"
          :items="outbounds.map(o => ({ label: `${flagFor(o.name)} ${o.name}${o.delay ? ` (${o.delay}ms)` : ''}`, value: o.name }))"
          class="w-44"
        />
        <UButton
          type="submit"
          :loading="adding"
          :disabled="!newValue.trim() || !newOutbound"
        >
          Добавить
        </UButton>
      </form>

      <!-- Direct outbounds row -->
      <div
        v-if="directOutbounds.length > 0"
        class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2"
      >
        <div
          v-for="ob in directOutbounds"
          :key="ob.name"
          class="border border-(--ui-border) rounded-md p-2 min-h-[120px] flex flex-col"
          :class="{ 'bg-(--ui-bg-elevated)': moving }"
          @dragover.prevent
          @drop.prevent="dropTo(ob.name)"
        >
          <div class="flex items-center justify-between mb-2 text-xs">
            <div class="flex items-center gap-1">
              <UIcon
                :name="rttColor(ob.delay) === 'success' ? 'i-lucide-circle' : 'i-lucide-circle-dot'"
                :class="`text-${rttColor(ob.delay)}-500`"
              />
              <span class="font-medium">{{ flagFor(ob.name) }} {{ ob.name }}</span>
            </div>
            <span class="text-(--ui-text-muted)">
              {{ ob.delay ? `${ob.delay}ms` : '—' }}
            </span>
          </div>
          <div class="flex-1 space-y-1">
            <div
              v-for="rule in rulesFor(ob.name)"
              :key="rule.id"
              :draggable="true"
              class="text-xs px-2 py-1 rounded border border-(--ui-border) bg-(--ui-bg) flex items-center justify-between gap-1 group cursor-grab"
              @dragstart="moving = { id: rule.id, value: rule.value }"
              @dragend="moving = null"
            >
              <span class="truncate">{{ rule.value }}</span>
              <UButton
                icon="i-lucide-x"
                size="xs"
                color="neutral"
                variant="ghost"
                class="opacity-0 group-hover:opacity-100"
                @click="deleteRule(rule)"
              />
            </div>
            <div
              v-if="rulesFor(ob.name).length === 0"
              class="text-(--ui-text-muted) text-xs text-center py-3 italic"
            >
              пусто — перетащи сюда
            </div>
          </div>
        </div>
      </div>

      <!-- WARP outbounds row -->
      <div
        v-if="warpOutbounds.length > 0"
        class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2"
      >
        <div
          v-for="ob in warpOutbounds"
          :key="ob.name"
          class="border border-(--ui-border) border-dashed rounded-md p-2 min-h-[120px] flex flex-col"
          :class="{ 'bg-(--ui-bg-elevated)': moving }"
          @dragover.prevent
          @drop.prevent="dropTo(ob.name)"
        >
          <div class="flex items-center justify-between mb-2 text-xs">
            <div class="flex items-center gap-1">
              <UIcon name="i-lucide-zap" class="text-yellow-500" />
              <span class="font-medium">{{ flagFor(ob.name) }} {{ ob.name }}</span>
            </div>
            <span class="text-(--ui-text-muted)">
              {{ ob.delay ? `${ob.delay}ms` : '—' }}
            </span>
          </div>
          <div class="flex-1 space-y-1">
            <div
              v-for="rule in rulesFor(ob.name)"
              :key="rule.id"
              :draggable="true"
              class="text-xs px-2 py-1 rounded border border-(--ui-border) bg-(--ui-bg) flex items-center justify-between gap-1 group cursor-grab"
              @dragstart="moving = { id: rule.id, value: rule.value }"
              @dragend="moving = null"
            >
              <span class="truncate">{{ rule.value }}</span>
              <UButton
                icon="i-lucide-x"
                size="xs"
                color="neutral"
                variant="ghost"
                class="opacity-0 group-hover:opacity-100"
                @click="deleteRule(rule)"
              />
            </div>
            <div
              v-if="rulesFor(ob.name).length === 0"
              class="text-(--ui-text-muted) text-xs text-center py-3 italic"
            >
              пусто — перетащи сюда
            </div>
          </div>
        </div>
      </div>

      <p class="text-xs text-(--ui-text-muted)">
        Перетаскивай правила между столбцами чтобы сменить outbound.
        Пустые столбцы → работает обычный geoip-роутинг. Ручные правила всегда побеждают.
      </p>
    </div>
  </UCard>
</template>
