<script setup lang="ts">
import Sortable from 'sortablejs'
import { defaultScreenLayout, SCREEN_MODULES } from '#shared/screen-layout'
import type { ScreenLayout } from '#shared/screen-layout'
import { useScreenLayout } from '~/composables/useScreenLayout'

useHead({ title: 'Настроить экран - anysda-vpn2' })

type Zone = keyof ScreenLayout

const { layout, save, loaded } = useScreenLayout()
const toast = useToast()
await loaded

const copy = (l: ScreenLayout): ScreenLayout => ({ left: [...l.left], right: [...l.right], hidden: [...l.hidden] })
const draft = ref<ScreenLayout>(copy(layout.value))

const columns: { id: Zone, label: string }[] = [
  { id: 'left', label: 'Левая колонка' },
  { id: 'right', label: 'Правая колонка' },
]

// Перетаскивает sortablejs (мышь и касание) между колонками и зоной «Скрыто».
// DOM он двигает сам, а рисует списки Vue: после броска плашка возвращается
// на место, порядок меняется в draft, и Vue перерисовывает зоны.
const zones = shallowReactive<Record<Zone, HTMLElement | null>>({ left: null, right: null, hidden: null })
watch(() => [zones.left, zones.right, zones.hidden], (els, _, onCleanup) => {
  const made = els.filter(el => el !== null).map((el) => {
    let next: Node | null = null
    return Sortable.create(el, {
      group: 'screen-layout',
      animation: 150,
      onStart: (e) => { next = e.item.nextSibling },
      onEnd: ({ item, from, to, oldIndex, newIndex }) => {
        from.insertBefore(item, next)
        if (oldIndex === undefined || newIndex === undefined) return
        const [id] = draft.value[from.dataset.zone as Zone].splice(oldIndex, 1)
        if (id) draft.value[to.dataset.zone as Zone].splice(newIndex, 0, id)
      },
    })
  })
  onCleanup(() => made.forEach(s => s.destroy()))
})

const saving = ref(false)
async function submit() {
  saving.value = true
  try {
    await save(draft.value)
    await navigateTo('/')
  }
  catch (e) {
    const err = e as { statusMessage?: string }
    toast.add({ title: err.statusMessage ?? 'Не удалось сохранить', color: 'error' })
  }
  finally {
    saving.value = false
  }
}
</script>

<template>
  <main class="tab on">
    <div class="head">
      <div>
        <h1>Настроить экран</h1>
        <p>Перетаскивай блоки обзора: порядок сверху вниз, ненужное — в «Скрыто»</p>
      </div>
    </div>
    <div class="max-w-xl mx-auto">

    <UCard>
      <div class="space-y-4">
        <div class="grid grid-cols-2 gap-3">
          <div v-for="column in columns" :key="column.id">
            <div class="text-xs text-(--ui-text-muted) mb-1.5">
              {{ column.label }}
            </div>
            <div
              :ref="(el: unknown) => { zones[column.id] = el as HTMLElement | null }"
              :data-zone="column.id"
              class="min-h-24 space-y-2 rounded-md border border-dashed border-(--ui-border-accented) p-2"
            >
              <div
                v-for="id in draft[column.id]"
                :key="id"
                :data-module="id"
                class="flex items-center gap-1.5 rounded-md border border-(--ui-border) bg-(--ui-bg-elevated) px-2 py-1.5 text-sm cursor-grab select-none"
              >
                <UIcon name="i-lucide-grip-vertical" class="hidden sm:block shrink-0 text-(--ui-text-muted)" />
                <span class="truncate">{{ SCREEN_MODULES[id].label }}</span>
              </div>
            </div>
          </div>
        </div>

        <div>
          <div class="text-xs text-(--ui-error) mb-1.5">
            Скрыто
          </div>
          <div
            :ref="(el: unknown) => { zones.hidden = el as HTMLElement | null }"
            data-zone="hidden"
            class="min-h-14 grid grid-cols-2 gap-2 content-start rounded-md border border-dashed border-(--ui-error)/60 bg-(--ui-error)/10 p-2"
          >
            <div
              v-for="id in draft.hidden"
              :key="id"
              :data-module="id"
              class="flex items-center gap-1.5 rounded-md border border-(--ui-border) bg-(--ui-bg-elevated) px-2 py-1.5 text-sm text-(--ui-text-muted) cursor-grab select-none"
            >
              <UIcon name="i-lucide-grip-vertical" class="hidden sm:block shrink-0" />
              <span class="truncate">{{ SCREEN_MODULES[id].label }}</span>
            </div>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="flex flex-wrap items-center justify-between gap-2">
          <UButton color="neutral" variant="ghost" @click="() => { draft = defaultScreenLayout() }">
            Сбросить по умолчанию
          </UButton>
          <div class="flex gap-2 ml-auto">
            <UButton color="neutral" variant="soft" to="/">
              Отмена
            </UButton>
            <UButton color="primary" :loading="saving" @click="submit">
              Сохранить
            </UButton>
          </div>
        </div>
      </template>
    </UCard>
    </div>
  </main>
</template>
