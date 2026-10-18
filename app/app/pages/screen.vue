<script setup lang="ts">
import Sortable from 'sortablejs'
import { defaultScreenLayout, SCREEN_MODULES } from '#shared/screen-layout'
import type { ScreenLayout, ScreenModuleId } from '#shared/screen-layout'
import { useScreenLayout } from '~/composables/useScreenLayout'

useHead({ title: 'Настроить экран - anysda-vpn2' })
definePageMeta({ title: 'Настроить экран', sub: 'Перетаскивай блоки обзора: порядок сверху вниз, ненужное — в «Скрыто»' })

type Zone = 'on' | 'off'

const { layout, save, loaded } = useScreenLayout()
const toast = useToast()
await loaded

// Обзор - одна колонка: «На обзоре» сверху вниз, остальное в «Скрыто».
const toDraft = (l: ScreenLayout): Record<Zone, ScreenModuleId[]> => ({ on: [...l.left, ...l.right], off: [...l.hidden] })
const draft = ref(toDraft(layout.value))
const isDefault = computed(() => draft.value.on.join() === toDraft(defaultScreenLayout()).on.join() && !draft.value.off.length)

// Перетаскивает sortablejs (мышь и касание) между зонами. DOM он двигает сам,
// а рисует списки Vue: после броска плашка возвращается на место, порядок
// меняется в draft, и Vue перерисовывает зоны.
const zones = shallowReactive<Record<Zone, HTMLElement | null>>({ on: null, off: null })
const over = ref<Zone | null>(null)
watch(() => [zones.on, zones.off], (els, _, onCleanup) => {
  const made = els.filter(el => el !== null).map((el) => {
    let next: Node | null = null
    return Sortable.create(el, {
      group: 'screen-layout',
      animation: 150,
      ghostClass: 'drag',
      onStart: (e) => { next = e.item.nextSibling },
      onMove: (e) => { over.value = (e.to as HTMLElement).dataset.zone as Zone },
      onEnd: ({ item, from, to, oldIndex, newIndex }) => {
        over.value = null
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
    await save({ left: draft.value.on, right: [], hidden: draft.value.off })
    toast.add({ title: 'Экран сохранён', color: 'success' })
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

const zoneList: { id: Zone, title: string }[] = [
  { id: 'on', title: 'На обзоре' },
  { id: 'off', title: 'Скрыто' },
]
</script>

<template>
  <main class="tab on scr">
    <div class="head">
      <div>
        <h1>Настроить экран</h1>
        <p>Перетаскивай блоки обзора: порядок сверху вниз, ненужное — в «Скрыто»</p>
      </div>
      <div class="ha">
        <button type="button" class="pill" :disabled="isDefault" @click="draft = toDraft(defaultScreenLayout())">
          Сбросить по умолчанию
        </button>
        <NuxtLink class="pill" to="/">
          Отмена
        </NuxtLink>
        <button type="button" class="pill w" :disabled="saving" @click="submit">
          Сохранить
        </button>
      </div>
    </div>
    <section class="scz">
      <div v-for="z in zoneList" :key="z.id" class="card zone" :class="{ hid: z.id === 'off' }">
        <h3>{{ z.title }}</h3>
        <div
          :ref="(el: unknown) => { zones[z.id] = el as HTMLElement | null }"
          class="zl"
          :class="{ over: over === z.id }"
          :data-zone="z.id"
          role="list"
          :aria-label="z.title"
        >
          <div v-for="id in draft[z.id]" :key="id" class="zb" role="listitem" :data-module="id">
            <div><b><WholeWords :text="SCREEN_MODULES[id].label" /></b><small><WholeWords :text="SCREEN_MODULES[id].hint" /></small></div>
          </div>
        </div>
      </div>
    </section>
  </main>
</template>

<style>
.scr .ha{display:flex;gap:8px}
.scr .scz{display:grid;grid-template-columns:1.4fr 1fr;gap:12px;align-items:start}
.scr .zone{min-height:360px}
.scr .card h3{font-size:14px;font-weight:500;color:var(--t2)}
.scr .zone.hid{border:1px dashed rgba(var(--red-rgb),.35);background:linear-gradient(180deg,rgba(var(--red-rgb),.04),var(--card-b))}
.scr .zl{display:flex;flex-direction:column;gap:8px;margin-top:14px;min-height:260px;border-radius:14px;transition:background .2s}
.scr .zl.over{background:rgba(var(--fg),.03)}
.scr .zb{display:flex;align-items:center;gap:12px;height:58px;padding:0 16px;border-radius:14px;background:var(--chip);border:1px solid var(--line);cursor:grab;user-select:none}
.scr .zb::before{content:"";width:6px;height:14px;opacity:.4;background:radial-gradient(circle,var(--t1) 1px,transparent 1.3px) 0 0/3px 3.4px}
.scr .zb b{font-weight:500;font-size:13.5px}
.scr .zb small{display:block;color:var(--t3);font-size:11.5px}
.scr .zb.drag{opacity:.35}
/* телефон: узкая колонка переносит название и подсказку по словам (WholeWords), плашка растёт по тексту */
@media (max-width:720px){
  .scr .zb{height:auto;min-height:58px;padding:10px 16px}
  .scr .zb>div{min-width:0}
  .scr .zb b{display:block}
}
</style>
