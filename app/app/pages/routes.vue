<script setup lang="ts">
import { plural } from '#shared/plural'
import type { Outbound } from '~/utils/routes-view'

interface Route { id: number, type: 'domain' | 'ip_cidr', value: string, outbound: string, createdAt: string }
interface Check { query: string, reason: keyof typeof WHY, rule: string, routeId: number | null, outbound: string, delay: number | null }
const WHY = {
  manual: 'своё правило',
  client: 'выбран у клиента',
  ru: 'российский адрес',
  youtube: 'YouTube через zapret',
  reject: 'заблокировано',
  rule: 'правило роутера',
  default: 'ни одно правило не подошло',
}
const EXAMPLE = 'chatgpt.com'

useHead({ title: 'Маршруты — anysda-vpn2' })
definePageMeta({ title: 'Маршруты' })
const { act } = useAct()
const mobile = useMobile()

const { data: rules, refresh } = useFetch<Route[]>('/api/routes', { default: () => [], server: false })
const { data: outbounds, refresh: refreshOutbounds } = useFetch<Outbound[]>('/api/routes/outbounds', { default: () => [], server: false })
usePoll(() => Promise.all([refresh(), refreshOutbounds()]), 4000)
const count = computed(() => `${rules.value.length} ${plural(rules.value.length, 'rule')}`)
usePageSub(() => `${count.value} · какой сайт через какой выход`)

const columns = computed(() => routeColumns(outbounds.value))
const cells = computed(() => columns.value.flatMap(c => c.cells))
const cellName = (ob: string) => cells.value.find(c => c.outbound === ob)?.name ?? ob
// Перенос виден сразу, до ответа сервера; при ошибке правило возвращается.
const moving = ref<Record<number, string>>({})
const shown = computed(() => rules.value.map(r => moving.value[r.id] ? { ...r, outbound: moving.value[r.id]! } : r))
const rulesFor = (ob: string) => shown.value.filter(r => r.outbound === ob)

async function move(rule: Route, outbound: string) {
  if (rule.outbound === outbound) return
  moving.value = { ...moving.value, [rule.id]: outbound }
  await act('move', async () => {
    await $fetch<unknown>(`/api/routes/${rule.id}`, { method: 'PATCH', body: { outbound } })
    await refresh()
  }, `«${rule.value}» → ${cellName(outbound)}`, 'Ошибка перемещения')
  const { [rule.id]: _, ...rest } = moving.value
  moving.value = rest
}
const remove = (rule: Route) => act('del', async () => {
  await $fetch<unknown>(`/api/routes/${rule.id}`, { method: 'DELETE' })
  await refresh()
}, `«${rule.value}» удалено`)

const dragging = ref<Route | null>(null)
const over = ref<string | null>(null)
function drop(ob: string) {
  if (dragging.value) void move(dragging.value, ob)
  dragging.value = over.value = null
}
// Телефон: перетаскивания нет, тап по правилу открывает выбор выхода.
const picked = ref<Route | null>(null)
const pickOpen = computed({ get: () => !!picked.value, set: (v) => { if (!v) picked.value = null } })
function pick(ob: string | null) {
  const r = picked.value!
  picked.value = null
  void (ob ? move(r, ob) : remove(r))
}

const newRule = reactive({ value: '', outbound: '' })
watch(cells, (list) => {
  if (!newRule.outbound && list.length) newRule.outbound = list.find(c => c.outbound === 'direct-ru')?.outbound ?? list[0]!.outbound
}, { immediate: true })
const add = () => act('add', async () => {
  await $fetch<unknown>('/api/routes', { method: 'POST', body: { value: newRule.value.trim(), outbound: newRule.outbound } })
  newRule.value = ''
  await refresh()
}, 'Правило добавлено')

// Проверка адреса: пустое поле - разбор примера серым; после правок правил - заново.
const query = ref('')
const checked = ref<Check | null>(null)
const checkError = ref('')
let seq = 0, timer: ReturnType<typeof setTimeout> | undefined
async function runCheck() {
  const my = ++seq
  try {
    const r = await $fetch<Check>('/api/routes/check', { query: { q: query.value.trim() || EXAMPLE } })
    if (my === seq) [checked.value, checkError.value] = [r, '']
  }
  catch (e) {
    if (my === seq) [checked.value, checkError.value] = [null, errText(e)]
  }
}
const schedule = (ms: number) => { clearTimeout(timer); timer = setTimeout(runCheck, ms) }
watch(query, () => schedule(250))
watch(() => rules.value.map(r => `${r.id}:${r.outbound}`).join(), () => schedule(0), { immediate: true })
onUnmounted(() => clearTimeout(timer))
const checkRule = computed(() => {
  const r = checked.value!
  return r.reason === 'default' ? 'по умолчанию' : r.reason === 'client' ? 'предпочитаемый экзит' : r.reason === 'manual' ? `правило ${r.rule}` : r.rule
})
const checkOut = computed(() => checked.value!.outbound === 'youtube-ru' ? `${countryName('ru')} · zapret` : cellName(checked.value!.outbound))
</script>

<template>
  <main class="tab on clp">
    <div class="head">
      <div>
        <h1>Маршруты</h1>
        <p>{{ count }} · какой сайт через какой выход</p>
      </div>
    </div>

    <section class="mb-3 grid gap-3 md:grid-cols-[7fr_5fr]">
      <div class="card">
        <h3 class="text-sm font-medium text-(--t2)">
          Проверить адрес
        </h3>
        <label class="inp big2"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" /></svg><input
          v-model="query" :placeholder="EXAMPLE" spellcheck="false" aria-label="Проверить адрес"></label>
        <div class="mt-4 flex min-h-10 flex-wrap items-center gap-2 text-sm" :class="{ 'opacity-45': !query.trim() }" data-testid="route-check">
          <template v-if="checked">
            <span class="tag">{{ checked.query }}</span>→<span class="tag">{{ checkRule }}</span>→
            <span class="tag" :class="{ badt: checked.delay === null }">{{ checkOut }} · {{ checked.delay ?? '—' }} мс</span>
            <span class="text-xs text-(--t3)">{{ WHY[checked.reason] }}</span>
          </template>
          <span v-else-if="checkError" class="text-xs text-(--t3)">{{ checkError }}</span>
        </div>
      </div>
      <form class="card" @submit.prevent="add">
        <h3 class="text-sm font-medium text-(--t2)">
          Новое правило
        </h3>
        <label class="inp big2"><input v-model="newRule.value" placeholder="netflix.com / *.openai.com / 8.8.8.8/32" spellcheck="false" aria-label="Новое правило"></label>
        <div class="mt-3 flex gap-2">
          <USelect v-model="newRule.outbound" :items="cells.map(c => ({ label: c.name, value: c.outbound }))" class="min-w-0 flex-1" size="lg" aria-label="Выход" />
          <button type="submit" class="pill w" :disabled="!newRule.value.trim() || !newRule.outbound">
            Добавить
          </button>
        </div>
      </form>
    </section>

    <section class="grid gap-3" :style="mobile ? undefined : { gridTemplateColumns: `repeat(${Math.min(Math.max(columns.length, 1), 5)},minmax(0,1fr))` }">
      <div v-for="col in columns" :key="col.tag" class="card flex flex-col gap-2.5">
        <div class="flex items-start justify-between gap-2">
          <div><b class="block font-medium">{{ col.name }}</b><small class="text-xs text-(--t3)">{{ col.sub }}</small></div>
          <span class="text-2xl font-extralight" :class="col.delay === null ? 'text-(--red-t)' : rtc(col.delay)">{{ col.delay ?? '—' }}<small class="text-xs">мс</small></span>
        </div>
        <div
          v-for="cell in col.cells" :key="cell.outbound" class="flex flex-col gap-1.5 rounded-xl border border-dashed p-2.5"
          :class="over === cell.outbound ? 'border-(--line2) bg-(--sunk2)' : 'border-(--line) bg-(--sunk)'"
          @dragover.prevent="over = cell.outbound" @drop.prevent="drop(cell.outbound)">
          <div class="flex justify-between gap-2 text-xs text-(--t3)">
            <span :class="{ 'text-(--orange)': cell.warp }">{{ cell.label }}</span><i class="not-italic" :class="rtc(cell.delay)">{{ cell.delay ?? '—' }} мс</i>
          </div>
          <div class="flex max-h-[146px] flex-col gap-1.5 overflow-y-auto max-md:max-h-none">
            <div
              v-for="rule in rulesFor(cell.outbound)" :key="rule.id" :draggable="!mobile" :role="mobile ? 'button' : undefined"
              class="group flex h-8 flex-none cursor-grab items-center gap-2 rounded-lg border bg-(--chip) px-2.5 text-xs"
              :class="[checked?.routeId === rule.id ? 'border-(--acc)' : 'border-(--line)', { 'opacity-35': dragging?.id === rule.id }]"
              @dragstart="dragging = rule" @dragend="dragging = over = null" @click="mobile && (picked = rule)">
              <span class="min-w-0 flex-1 truncate">{{ rule.value }}</span>
              <button type="button" class="opacity-0 group-hover:opacity-100 hover:text-(--red-t) max-md:hidden" aria-label="Удалить" @click="remove(rule)">
                ✕
              </button>
            </div>
            <span v-if="!rulesFor(cell.outbound).length" class="m-auto py-2 text-xs text-(--t4)">{{ mobile ? 'пусто' : 'перетащите сюда' }}</span>
          </div>
        </div>
      </div>
    </section>
    <p class="mt-3 text-sm text-(--t3)">
      {{ mobile ? 'Нажми на правило, чтобы перенести его на другой выход.' : 'Перетаскивай правила между выходами.' }} Трафик без совпадения с ручным правилом идёт по автоматическим правилам роутера.
    </p>

    <PanelDialog v-model:open="pickOpen" :title="picked?.value ?? ''">
      <p class="mt">
        Выход
      </p>
      <button
        v-for="c in cells" :key="c.outbound" type="button" class="pill mt-2 w-full justify-between" :class="{ on: c.outbound === picked?.outbound }"
        :aria-pressed="c.outbound === picked?.outbound" @click="pick(c.outbound)">
        <span :class="{ 'text-(--orange)': c.warp }">{{ c.name }}</span><i class="not-italic" :class="rtc(c.delay)">{{ c.delay ?? '—' }} мс</i>
      </button>
      <template #footer>
        <button type="button" class="pill red w-full justify-center" @click="pick(null)">
          Удалить
        </button>
      </template>
    </PanelDialog>
  </main>
</template>
