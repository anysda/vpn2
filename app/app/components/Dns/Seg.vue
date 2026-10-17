<script setup lang="ts" generic="T extends string">
// Переключатель макета (.seg) со скользящей пилюлей под выбранной кнопкой.
const props = defineProps<{ options: ReadonlyArray<{ value: T, label: string }>, small?: boolean }>()
const model = defineModel<T>({ required: true })
const emit = defineEmits<{ pick: [value: T, dir: 'fwd' | 'back'] }>()

const box = ref<HTMLElement>()
const glided = ref(false)

function glide(jump = false) {
  const b = box.value
  const g = b?.querySelector<HTMLElement>(':scope>.glide')
  const on = b?.querySelector<HTMLElement>(':scope>button.on')
  if (!b || !g || !on || !b.offsetWidth) return
  jump ||= !glided.value || reducedMotion()
  g.classList.toggle('now', jump)
  const r = on.getBoundingClientRect(), q = b.getBoundingClientRect()
  g.style.height = `${r.height}px`
  g.style.width = `${r.width}px`
  g.style.transform = `translate(${r.left - q.left - b.clientLeft + b.scrollLeft}px,${r.top - q.top - b.clientTop + b.scrollTop}px)`
  if (jump) requestAnimationFrame(() => g.classList.remove('now'))
  glided.value = true
}

function click(v: T) {
  if (v === model.value) return
  const idx = (x: T) => props.options.findIndex(o => o.value === x)
  emit('pick', v, idx(v) > idx(model.value) ? 'fwd' : 'back')
}

// Ряд шире окна (телефон) прокручивается сам: выбранная кнопка — в середину.
function reveal() {
  const b = box.value
  const on = b?.querySelector<HTMLElement>(':scope>button.on')
  if (!b || !on || b.scrollWidth <= b.clientWidth) return
  b.scrollTo({ left: on.offsetLeft - (b.clientWidth - on.offsetWidth) / 2, behavior: reducedMotion() ? 'auto' : 'smooth' })
}

watch(model, () => { glide(); reveal() }, { flush: 'post' })
const onResize = () => glide(true)
onMounted(() => {
  requestAnimationFrame(() => glide(true))
  void document.fonts?.ready.then(() => glide(true))
  addEventListener('resize', onResize)
})
onUnmounted(() => removeEventListener('resize', onResize))
defineExpose({ glide })
</script>

<template>
  <div ref="box" class="seg" :class="{ sm: small, gl: glided }" role="tablist">
    <button
      v-for="o in options"
      :key="o.value"
      type="button"
      role="tab"
      :class="{ on: o.value === model }"
      :aria-selected="o.value === model"
      @click="click(o.value)"
    >
      {{ o.label }}
    </button>
    <span class="glide now" aria-hidden="true" />
  </div>
</template>
