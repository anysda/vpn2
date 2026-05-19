<script setup lang="ts">
import type { Outbound, Route } from '~/composables/useRoutes'
import { flagFor, parseOutbound, pluralRu, rttColor, useRoutes } from '~/composables/useRoutes'

const { rules, outbounds, create, patch, remove } = useRoutes()
const toast = useToast()

const newValue = ref('')
const newOutbound = ref<string>('')
const adding = ref(false)

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

interface Column {
  tag: string
  direct: Outbound | null
  warp: Outbound | null
}

const columns = computed<Column[]>(() => {
  const byTag = new Map<string, Column>()
  for (const ob of outbounds.value) {
    const parsed = parseOutbound(ob.name)
    if (!parsed) continue
    let col = byTag.get(parsed.tag)
    if (!col) {
      col = { tag: parsed.tag, direct: null, warp: null }
      byTag.set(parsed.tag, col)
    }
    if (parsed.variant === 'direct') col.direct = ob
    else col.warp = ob
  }
  // RU first, then alphabetical by tag — same order as Monitoring node grid
  const arr = Array.from(byTag.values())
  return arr.sort((a, b) => {
    if (a.tag === 'ru') return -1
    if (b.tag === 'ru') return 1
    return a.tag.localeCompare(b.tag)
  })
})

function rulesFor(outboundName: string | null | undefined): Route[] {
  if (!outboundName) return []
  return rules.value.filter(r => r.outbound === outboundName)
}

// Drag state
const dragging = ref<{ id: number, value: string } | null>(null)
const dragOver = ref<string | null>(null)

function onDragStart(rule: Route) {
  dragging.value = { id: rule.id, value: rule.value }
}
function onDragEnd() {
  dragging.value = null
  dragOver.value = null
}
function onDragOver(target: string, e: DragEvent) {
  e.preventDefault()
  if (dragOver.value !== target) dragOver.value = target
}
async function onDrop(targetOutbound: string) {
  const m = dragging.value
  dragging.value = null
  dragOver.value = null
  if (!m) return
  const cur = rules.value.find(r => r.id === m.id)
  if (!cur || cur.outbound === targetOutbound) return
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

function rttBadge(delay: number | null) {
  if (delay === null) return { text: '—', cls: 'text-(--ui-text-dimmed)' }
  const color = rttColor(delay)
  const map: Record<string, string> = {
    success: 'text-violet-400',
    warning: 'text-amber-400',
    error: 'text-rose-400',
    neutral: 'text-(--ui-text-muted)',
  }
  return { text: `${delay}ms`, cls: map[color] }
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
          {{ rules.length }} {{ pluralRu(rules.length, ['правило', 'правила', 'правил']) }}
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
            placeholder="netflix.com / *.openai.com / 8.8.8.8/32"
            class="w-full"
          />
        </div>
        <USelect
          v-model="newOutbound"
          :items="outbounds.map(o => {
            const p = parseOutbound(o.name)
            const label = p
              ? `${flagFor(p.tag)} ${p.tag.toUpperCase()}${p.variant === 'warp' ? ' WARP' : ''}`
              : o.name
            return { label, value: o.name }
          })"
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

      <!-- Columns: one per exit, direct cell on top, warp cell below -->
      <div
        v-if="columns.length > 0"
        class="grid gap-2"
        :style="`grid-template-columns: repeat(${columns.length}, minmax(120px, 1fr))`"
      >
        <div
          v-for="col in columns"
          :key="col.tag"
          class="space-y-2"
        >
          <!-- Direct cell -->
          <div
            v-if="col.direct"
            :class="[
              'rounded-md p-2 min-h-[121px] flex flex-col border transition-colors',
              dragOver === col.direct.name
                ? 'border-violet-500 bg-violet-500/10'
                : 'border-(--ui-border) bg-(--ui-page)',
            ]"
            @dragover="onDragOver(col.direct.name, $event)"
            @drop.prevent="onDrop(col.direct.name)"
          >
            <div class="flex items-center justify-between mb-1.5 text-xs">
              <span class="font-semibold">
                {{ flagFor(col.tag) }} {{ col.tag.toUpperCase() }}
              </span>
              <span :class="rttBadge(col.direct.delay).cls">
                {{ rttBadge(col.direct.delay).text }}
              </span>
            </div>
            <div class="flex-1 space-y-1">
              <div
                v-for="rule in rulesFor(col.direct.name)"
                :key="rule.id"
                draggable="true"
                class="text-xs px-2 py-1 rounded border border-(--ui-border) bg-(--ui-bg-muted) flex items-center justify-between gap-1 group cursor-grab"
                @dragstart="onDragStart(rule)"
                @dragend="onDragEnd"
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
            </div>
          </div>

          <!-- Warp cell (under direct, same column) -->
          <div
            v-if="col.warp"
            :class="[
              'rounded-md p-2 min-h-[80px] flex flex-col border transition-colors',
              dragOver === col.warp.name
                ? 'border-violet-500 bg-violet-500/10'
                : 'border-(--ui-border) bg-(--ui-page)',
            ]"
            @dragover="onDragOver(col.warp.name, $event)"
            @drop.prevent="onDrop(col.warp.name)"
          >
            <div class="flex items-center justify-between mb-1.5 text-xs">
              <span class="font-medium flex items-center gap-1">
                {{ flagFor(col.tag) }} <span class="text-[#f38020]">{{ col.tag.toUpperCase() }} WARP</span>
              </span>
              <span :class="rttBadge(col.warp.delay).cls">
                {{ rttBadge(col.warp.delay).text }}
              </span>
            </div>
            <div class="flex-1 space-y-1">
              <div
                v-for="rule in rulesFor(col.warp.name)"
                :key="rule.id"
                draggable="true"
                class="text-xs px-2 py-1 rounded border border-(--ui-border) bg-(--ui-bg-muted) flex items-center justify-between gap-1 group cursor-grab"
                @dragstart="onDragStart(rule)"
                @dragend="onDragEnd"
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
            </div>
          </div>
        </div>
      </div>

      <p class="text-xs text-(--ui-text-muted)">
        Перетаскивай правила между выходами. Пустые ячейки → geoip-роутинг по умолчанию.
      </p>
    </div>
  </UCard>
</template>
