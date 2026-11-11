<script setup lang="ts">
import type { Route } from '~/composables/useRoutes'
import { useRoutes } from '~/composables/useRoutes'
import { routeColumns, rtc } from '~/utils/routes-view'

const { rules, outbounds, create, patch, remove } = useRoutes()
const toast = useToast()
// На телефоне перетаскивания нет: тап по правилу открывает шторку выбора выхода.
const mobile = useMobile()

const EASE = 'cubic-bezier(.23,1,.32,1)'
const reduced = () => import.meta.client && matchMedia('(prefers-reduced-motion: reduce)').matches

const columns = computed(() => routeColumns(outbounds.value))
const cells = computed(() => columns.value.flatMap(c => c.cells))
const cellName = (outbound: string) => cells.value.find(c => c.outbound === outbound)?.name ?? outbound

// Перенос показывается сразу, до ответа сервера; при ошибке правило
// возвращается в прежнюю ячейку.
const moving = ref(new Map<number, string>())
function setMoving(id: number, outbound: string | null) {
  const next = new Map(moving.value)
  if (outbound === null) next.delete(id)
  else next.set(id, outbound)
  moving.value = next
}

const shown = computed(() => rules.value.map(r => moving.value.has(r.id) ? { ...r, outbound: moving.value.get(r.id)! } : r))
function rulesFor(outbound: string): Route[] {
  return shown.value.filter(r => r.outbound === outbound)
}

// ---------- новое правило и проверка адреса ----------
const fresh = ref<number | null>(null)
// Правило, под которое попал проверенный адрес.
const hit = ref<number | null>(null)
const board = ref<HTMLElement | null>(null)

const chipEl = (id: number) => board.value?.querySelector<HTMLElement>(`.rc[data-id="${id}"]`) ?? null

async function onAdded(id: number) {
  fresh.value = id
  await nextTick()
  // Добавленное правило должно быть видно и в длинной ячейке.
  const list = chipEl(id)?.parentElement
  if (list) list.scrollTop = list.scrollHeight
}

// ---------- перетаскивание ----------
const dragging = ref<number | null>(null)
const over = ref<string | null>(null)

function onDragStart(rule: Route, e: DragEvent) {
  dragging.value = rule.id
  fresh.value = null
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', rule.value)
  }
}
function onDragEnd() {
  dragging.value = null
  over.value = null
}
function onDragOver(outbound: string, e: DragEvent) {
  if (dragging.value === null) return
  e.preventDefault()
  over.value = outbound
}

async function onDrop(outbound: string, e: DragEvent) {
  const id = dragging.value
  if (id === null) return
  e.preventDefault()
  dragging.value = null
  over.value = null
  const rule = shown.value.find(r => r.id === id)
  if (!rule || rule.outbound === outbound) return
  const { clientX: x0, clientY: y0 } = e
  // Правило ложится в ячейку из точки, где его отпустили.
  if (!reduced()) {
    void nextTick(() => requestAnimationFrame(() => {
      const el = chipEl(id)
      if (!el) return
      const r = el.getBoundingClientRect()
      el.animate([
        { transform: `translate(${x0 - r.left - r.width / 2}px,${y0 - r.top - r.height / 2}px) scale(1.04)`, boxShadow: '0 12px 30px rgba(0,0,0,.5)' },
        { transform: 'none', boxShadow: '0 0 0 rgba(0,0,0,0)' },
      ], { duration: 380, easing: EASE })
    }))
  }
  await move(rule, outbound)
}

// Перенос на сервере — общий для перетаскивания и шторки телефона.
async function move(rule: Route, outbound: string) {
  const id = rule.id
  setMoving(id, outbound)
  try {
    await patch(id, outbound)
    toast.add({ title: `«${rule.value}» → ${cellName(outbound)}`, color: 'success' })
  }
  catch (err) {
    toast.add({ title: 'Ошибка перемещения', color: 'error', description: (err as Error).message })
  }
  finally {
    setMoving(id, null)
  }
}

// ---------- шторка правила (телефон) ----------
const picked = ref<number | null>(null)
const pickedRule = computed(() => shown.value.find(r => r.id === picked.value) ?? null)
// Последнее открытое правило остаётся в шторке, пока она уезжает вниз.
const sheetRule = ref<Route | null>(null)
watch(pickedRule, (r) => { if (r) sheetRule.value = r })

function onRuleTap(rule: Route) {
  if (mobile.value) picked.value = rule.id
}
function pickOutbound(outbound: string) {
  const rule = pickedRule.value
  picked.value = null
  if (!rule || rule.outbound === outbound) return
  // Без точки отпускания правило просто проявляется в новой ячейке.
  fresh.value = rule.id
  void move(rule, outbound)
}
function deletePicked() {
  const rule = pickedRule.value
  picked.value = null
  if (rule) void deleteRule(rule)
}

// ---------- удаление ----------
async function deleteRule(rule: Route) {
  const el = chipEl(rule.id)
  let anim: Animation | null = null
  if (el && !reduced()) {
    // Удалённое схлопывается до запроса, чтобы ответ сервера не обрывал движение.
    el.style.pointerEvents = 'none'
    anim = el.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.94)' }], { duration: 160, easing: EASE, fill: 'forwards' })
    await anim.finished.catch(() => {})
  }
  try {
    await remove(rule.id)
    toast.add({ title: `«${rule.value}» удалено`, color: 'success' })
  }
  catch (e) {
    anim?.cancel()
    if (el) el.style.pointerEvents = ''
    toast.add({ title: 'Ошибка', color: 'error', description: (e as Error).message })
  }
}
</script>

<template>
  <RoutesTop :rules="rules" :cells="cells" :create="create" @hit="hit = $event" @added="onAdded" />

  <section ref="board" class="board" :style="{ gridTemplateColumns: `repeat(${Math.min(Math.max(columns.length, 1), 5)},minmax(min-content,1fr))` }">
    <div v-for="col in columns" :key="col.tag" :class="['card', 'ex', { badx: col.delay === null }]">
      <div class="eh">
        <div>
          <b>{{ col.name }}</b><small>{{ col.sub }}</small>
        </div>
        <span :class="['rt', rtc(col.delay)]">{{ col.delay ?? '—' }}<small>мс</small></span>
      </div>
      <div
        v-for="cell in col.cells" :key="cell.outbound"
        :class="['cell', { empty: !rulesFor(cell.outbound).length, warp: cell.warp, over: over === cell.outbound }]" :data-outbound="cell.outbound"
        @dragover="onDragOver(cell.outbound, $event)" @drop="onDrop(cell.outbound, $event)">
        <div class="cl">
          <span>{{ cell.label }}</span><i :class="rtc(cell.delay)">{{ cell.delay ?? '—' }} мс</i>
        </div>
        <div :class="['rcs', { scroll: rulesFor(cell.outbound).length > 4 }]">
          <div
            v-for="rule in rulesFor(cell.outbound)" :key="rule.id"
            :class="['rc', { drag: dragging === rule.id, new: fresh === rule.id, hit: hit === rule.id }]" :data-id="rule.id" :draggable="!mobile"
            :role="mobile ? 'button' : undefined" :tabindex="mobile ? 0 : undefined" :aria-haspopup="mobile ? 'dialog' : undefined"
            @dragstart="onDragStart(rule, $event)" @dragend="onDragEnd" @click="onRuleTap(rule)" @keydown.enter="onRuleTap(rule)">
            <span class="rtx">{{ rule.value }}</span><button class="x" type="button" aria-label="Удалить" @click="deleteRule(rule)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M6 6l12 12M18 6L6 18" /></svg>
            </button>
          </div>
        </div>
      </div>
    </div>
  </section>
  <p class="hint">
    {{ mobile ? 'Нажми на правило, чтобы перенести его на другой выход.' : 'Перетаскивай правила между выходами.' }} Трафик без совпадения с ручным правилом идёт по автоматическим правилам роутера.
  </p>
  <BottomSheet v-if="mobile" :open="!!pickedRule" label="Перенос правила" @close="picked = null">
    <template v-if="sheetRule">
      <h2 class="rsh">
        {{ sheetRule.value }}
      </h2>
      <p class="rsp">
        Выход
      </p>
      <button
        v-for="c in cells" :key="c.outbound" type="button" :class="['ro', { on: c.outbound === sheetRule.outbound, warp: c.warp }]"
        :aria-pressed="c.outbound === sheetRule.outbound" @click="pickOutbound(c.outbound)">
        <span>{{ c.name }}</span><i :class="rtc(c.delay)">{{ c.delay ?? '—' }} мс</i>
      </button>
      <button type="button" class="pill red rdel" @click="deletePicked">
        Удалить
      </button>
    </template>
  </BottomSheet>
</template>

<style scoped>
.board{display:grid;grid-template-columns:repeat(5,minmax(min-content,1fr));gap:12px}
/* Шапка, «Напрямую», «WARP» - строки общей сетки доски: шапка в две строки
   («Соединенные Штаты» на узком окне) не сдвигает и не растягивает ячейки
   соседних колонок. Единственная ячейка колонки (Россия) занимает обе строки. */
/* Колонка - доля доски, но не уже шапки: имя страны переносится только по
   словам, самое длинное слово и задержка (flex:none) видны целиком. Ячейки
   в ширину колонки не вмешиваются (contain:inline-size): правило и подпись
   режутся многоточием. */
.ex{padding:14px;display:grid;grid-row:span 3;grid-template-rows:subgrid;grid-template-columns:minmax(min-content,1fr);row-gap:10px}
.ex .cell:nth-child(2):last-child{grid-row:span 2}
/* Имя занимает всё место рядом с задержкой; задержка уходит под имя, только если
   рядом с ней не встаёт самое длинное слово имени. */
.ex .eh{display:flex;flex-wrap:wrap;column-gap:8px;justify-content:space-between;align-items:flex-start;padding:2px 2px 4px}
.ex .eh>div{flex:1 1 0;min-width:min-content}
.ex .eh b{font-weight:500;font-size:15px;display:block}
.ex .eh small{font-size:11.5px;color:var(--t3)}
.ex .eh .rt{flex:none;white-space:nowrap;font-weight:200;font-size:26px;letter-spacing:-.02em;line-height:1}
.ex .eh .rt small{font-size:12px;margin-left:2px}
.ex.badx .eh .rt{color:var(--red-t)}
.cell{contain:inline-size;border-radius:14px;background:var(--sunk);border:1px dashed rgba(var(--fg),.08);box-shadow:inset 0 2px 12px rgba(0,0,0,calc(.5*var(--shk)));padding:10px;display:flex;flex-direction:column;gap:6px;transition:border-color .2s,background .2s}
.cell.over{border-color:rgba(var(--fg),.45);background:var(--sunk2)}
.cell .cl{display:flex;justify-content:space-between;gap:8px;font-size:11.5px;color:var(--t3);padding:0 2px 2px}
.cell .cl span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cell .cl i{flex:none;white-space:nowrap}
.cell .cl i{font-style:normal}
.cell.warp .cl span{color:var(--orange)}
.rc{position:relative;display:flex;align-items:center;gap:8px;height:32px;padding:0 10px;border-radius:9px;background:var(--chip);border:1px solid var(--line);font-size:12px;color:var(--t1);cursor:grab;user-select:none;transition:transform .16s var(--ease),opacity .2s,border-color .2s;white-space:nowrap;overflow:hidden}
/* Многоточие только у блока: в flex-контейнере текст режется краем. */
.rc .rtx{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
.rc:hover{border-color:var(--line2)}.rc:active{cursor:grabbing}
.rc.drag{opacity:.35}
.rc.hit{border-color:var(--acc);box-shadow:0 0 16px rgba(var(--glow),calc(.25*var(--gk)))}
.rc.new{animation:pop .5s var(--ease)}
@keyframes pop{from{opacity:0;transform:scale(.94)}to{opacity:1;transform:none}}
.rc::before{content:"";width:6px;height:10px;flex:none;opacity:.35;background:radial-gradient(circle,var(--t1) 1px,transparent 1.3px) 0 0/3px 3.4px}
.rc .x{position:absolute;right:4px;top:50%;width:22px;height:22px;margin-top:-11px;border-radius:7px;border:0;background:var(--tog);color:var(--t2);display:grid;place-items:center;cursor:pointer;opacity:0;transition:opacity .15s,color .15s}
.rc:hover .x{opacity:1}.rc .x:hover{color:var(--red-t)}.rc .x svg{width:11px;height:11px}
/* Место под список - ровно четыре правила (4×32 + 3×6): ячейка не растёт и не
   сжимается от числа правил, лишние прокручиваются внутри. */
.rcs{flex:none;height:146px;min-height:0;display:flex;flex-direction:column;gap:6px}.rcs .rc{flex:none}
.cell.empty .rcs::after{content:"перетащите сюда";margin:auto;font-size:11.5px;color:var(--t4)}
.rcs.scroll{margin-right:-10px;overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;scrollbar-width:thin;scrollbar-color:rgba(var(--fg),.14) transparent}
.rcs.scroll::-webkit-scrollbar{width:8px}.rcs.scroll::-webkit-scrollbar-thumb{background:rgba(var(--fg),.12);border-radius:8px;border:2px solid transparent;background-clip:padding-box}
.rtv{color:var(--violet)!important}.rta{color:var(--amber)!important}.rtp{color:var(--pink)!important}
.hint{margin-top:12px;color:var(--t3);font-size:12.5px}
@media (max-width:1360px){
  .ex .eh .rt{font-size:22px}
}
/* Телефон: карточки друг под другом, экзиты списком, перенос — тапом и шторкой. */
@media (max-width:720px){
  /* !important: число колонок десктопа задано на элементе */
  .board{grid-template-columns:minmax(0,1fr)!important}
  .ex{grid-row:auto;grid-template-rows:none;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px}
  .ex .eh{grid-column:1/-1;padding:2px}
  .ex .eh .rt{font-size:26px}
  .ex .cell:nth-child(2):last-child{grid-column:1/-1;grid-row:auto}
  .cell{min-width:0}
  .rcs,.rcs.scroll{height:auto;margin-right:0;overflow:visible}
  .cell.empty .rcs::after{content:"пусто";margin:0;line-height:32px;text-align:center}
  .rc{cursor:pointer;touch-action:manipulation;-webkit-touch-callout:none}
  .rc::before,.rc .x{display:none}
  .rc:not(.hit):hover{border-color:var(--line)}
  .rc:active{cursor:pointer;transform:scale(.97)}
  .hint{margin:0 4px}
}
/* шторка правила */
.rsh{margin:0;font-size:18px;font-weight:500;letter-spacing:-.01em;word-break:break-all}
.rsp{margin:4px 0 8px;color:var(--t3);font-size:12.5px}
.ro{display:flex;align-items:center;gap:10px;width:100%;height:44px;padding:0 14px;margin-top:6px;border-radius:14px;border:1px solid var(--line);background:rgba(var(--fg),.03);color:var(--t1);font:500 13px var(--font-sans);text-align:left;cursor:pointer;transition:transform .16s var(--ease),background-color .2s,border-color .2s}
.ro span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ro.warp span{color:var(--orange)}
.ro i{flex:none;font-style:normal;font-size:12px;color:var(--t3)}
.ro.on{border-color:var(--acc);background:rgba(var(--fg),.08)}
.ro:active{transform:scale(.98)}
.pill.red{color:var(--red-t);border-color:rgba(var(--red-rgb),.3)}
.rdel{width:100%;height:46px;margin-top:16px;justify-content:center}
</style>
