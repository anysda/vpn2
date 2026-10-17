<script setup lang="ts">
// Нижняя шторка телефона: меню профиля, уведомления и мобильные окна
// разделов. Закрывается тапом по фону, Escape и системным «назад»: открытая
// шторка — своя запись в истории (useBackStack). Кнопки — в слоте foot.
const props = defineProps<{ open: boolean, label: string }>()
const emit = defineEmits<{ close: [] }>()
const panel = ref<HTMLElement>()
const router = useRouter()
const stack = useBackStack()
const uid = useId()
let seq = 0
/** Метка записи открытой шторки и её позиция в истории; null — записи нет. */
let layer: string | null = null
let pos = Infinity

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}
function onPop() {
  if (layer && Number(history.state?.position) < pos) {
    forget()
    emit('close')
  }
}
function forget() {
  layer = null
  pos = Infinity
  removeEventListener('popstate', onPop)
}
function enter() {
  const l = layer = `${uid}-${++seq}`
  addEventListener('popstate', onPop)
  void stack.open(l).then((p) => { if (layer === l) pos = p })
}
function leave() {
  const l = layer
  forget()
  if (l) void stack.close(l)
}
// Пока шторка выезжает, пункт уходит из-под пальца, и клик приходит в её поле.
// Срабатывает то, чего палец коснулся, как в системных шторках. Мышь не
// трогаем: увести курсор с кнопки до отпускания — отказ от нажатия.
const ACTIVE = 'a, button, input, select, textarea, label, [role=menuitem]'
let pressed: HTMLElement | null = null
function onPress(e: PointerEvent) {
  pressed = e.pointerType === 'mouse' ? null : (e.target as Element).closest?.<HTMLElement>(ACTIVE) ?? null
}
function onClick(e: MouseEvent) {
  const el = pressed
  pressed = null
  if (el?.isConnected && !(e.target as Element).closest?.(ACTIVE)) {
    e.stopPropagation()
    el.click()
    return
  }
  onLink(e)
}
// Пункт-ссылка внутри шторки: переход заменяет её запись, а не ложится сверху.
function onLink(e: MouseEvent) {
  const a = (e.target as Element).closest?.('a[href]')
  const href = a?.getAttribute('href')
  if (!layer || !a || !href?.startsWith('/') || a.hasAttribute('download') || a.getAttribute('target')
    || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey
    || !router.resolve(href).matched.length) return
  e.preventDefault()
  const l = layer
  forget()
  void stack.push(href, l)
}
function lock(on: boolean) {
  document.documentElement.style.overflow = on ? 'hidden' : ''
  if (on) addEventListener('keydown', onKey)
  else removeEventListener('keydown', onKey)
}
watch(() => props.open, (on) => {
  lock(on)
  if (on) {
    enter()
    nextTick(() => panel.value?.focus({ preventScroll: true }))
  }
  else leave()
})
onMounted(() => {
  if (!props.open) return
  lock(true)
  enter()
})
onUnmounted(() => {
  if (props.open) lock(false)
  leave()
})
</script>

<template>
  <Teleport to="#teleports">
    <div class="bso" :class="{ on: open }" aria-hidden="true" @click="emit('close')" />
    <div
      ref="panel"
      class="bsh"
      :class="{ on: open }"
      role="dialog"
      aria-modal="true"
      :aria-label="label"
      :aria-hidden="!open"
      :inert="!open"
      tabindex="-1"
      @pointerdown.capture="onPress"
      @click.capture="onClick"
    >
      <slot />
      <div v-if="$slots.foot" class="bsf">
        <slot name="foot" />
      </div>
    </div>
  </Teleport>
</template>
