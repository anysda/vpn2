<script setup lang="ts">
import '~/assets/css/clients.css'
import { pluralRu } from '#shared/plural'
import { expiryLabel, useClients } from '~/composables/useClients'
import type { Client, ClientTraffic } from '~/composables/useClients'
import { useExitTags } from '~/composables/useRoutes'
import { anim, expiringInDays, inDaysRu, initials, sizeRu } from '~/utils/clients-view'

useHead({ title: 'Клиенты — anysda-vpn2' })

const { clients, refresh: refreshClients } = useClients()
const exitTags = useExitTags()

const { data: trafficMap, refresh: refreshTraffic } = useFetch<Record<number, ClientTraffic>>('/api/clients/traffic', {
  default: () => ({}),
  server: false,
})
let trafficTimer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  if (!trafficTimer) trafficTimer = setInterval(() => { void refreshTraffic() }, 3000)
})
onUnmounted(() => { if (trafficTimer) { clearInterval(trafficTimer); trafficTimer = null } })
useVisibleRefresh(refreshTraffic)

const subtitle = computed(() => {
  const n = clients.value.length
  const d = clients.value.reduce((sum, c) => sum + c.deviceCount, 0)
  return `${n} ${pluralRu(n, ['клиент', 'клиента', 'клиентов'])} · ${d} ${pluralRu(d, ['устройство', 'устройства', 'устройств'])}`
})

type Filter = 'all' | 'exp' | 'frz'
const filter = ref<Filter>('all')
const search = ref('')

const traffic = (id: number) => trafficMap.value?.[id] ?? null
const expiring = (c: Client) => expiringInDays(c.expiresAt, c.status)

const counts = computed(() => ({
  all: clients.value.length,
  exp: clients.value.filter(c => expiring(c) !== null).length,
  frz: clients.value.filter(c => c.status === 'frozen').length,
}))

// Сверху — кто «в сети» (трафик за 5 мин), дальше по объёму за сутки, затем по имени.
const sorted = computed(() => {
  const day = (id: number) => (traffic(id)?.dayRx ?? 0) + (traffic(id)?.dayTx ?? 0)
  return [...clients.value].sort((a, b) =>
    Number(traffic(b.id)?.online ?? false) - Number(traffic(a.id)?.online ?? false)
    || day(b.id) - day(a.id)
    || a.name.localeCompare(b.name, 'ru'),
  )
})

const rows = computed(() => {
  const q = search.value.trim().toLowerCase()
  return sorted.value.filter(c =>
    (filter.value === 'all' || (filter.value === 'exp' ? expiring(c) !== null : c.status === 'frozen'))
    && (!q || `${c.name}${c.tgUsername ? `@${c.tgUsername}` : ''}`.toLowerCase().includes(q)),
  )
})

// Выбранный клиент: из ссылки ?id= (топ на Обзоре), иначе первый в списке.
const selectedId = ref<number | null>(Number(useRoute().query.id) || null)
watch(sorted, (list) => {
  if (selectedId.value !== null && list.some(c => c.id === selectedId.value)) return
  selectedId.value = list[0]?.id ?? null
}, { immediate: true })

function select(id: number) {
  selectedId.value = id
}

async function onCreated(id: number) {
  await refreshClients()
  selectedId.value = id
}

async function onDeleted() {
  selectedId.value = null
  await refreshClients()
}

// Клиента удалили, пока грузилась карточка: выбрать соседа по списку, без тоста.
async function onGone(id: number) {
  if (selectedId.value !== id) return
  const list = sorted.value
  const at = list.findIndex(c => c.id === id)
  const rest = list.filter(c => c.id !== id)
  selectedId.value = rest[Math.min(Math.max(at, 0), rest.length - 1)]?.id ?? null
  await refreshClients()
}

// Строки списка появляются каскадом при смене фильтра или поиска.
const rowsEl = ref<HTMLElement>()
watch([filter, search], () => nextTick(() => {
  ;[...(rowsEl.value?.querySelectorAll('.crow') ?? [])].slice(0, 12).forEach((r, i) =>
    anim(r, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], { duration: 320, delay: i * 28 }))
}))

// Подложка активного сегмента ездит за кнопкой, как в шапке.
const seg = ref<HTMLElement>()
const glideEl = ref<HTMLElement>()
const glided = ref(false)
function glide(jump = false) {
  const box = seg.value, g = glideEl.value
  const on = box?.querySelector<HTMLElement>(':scope>.on')
  if (!box || !g || !on || !box.offsetWidth) return
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches
  g.classList.toggle('now', jump || !glided.value || reduce)
  const r = on.getBoundingClientRect(), q = box.getBoundingClientRect()
  g.style.width = `${r.width}px`
  g.style.height = `${r.height}px`
  g.style.transform = `translate(${r.left - q.left - box.clientLeft}px,${r.top - q.top - box.clientTop}px)`
  if (!glided.value || jump) requestAnimationFrame(() => g.classList.remove('now'))
  glided.value = true
}
const onResize = () => glide(true)
watch(filter, () => nextTick(() => requestAnimationFrame(() => glide())))
watch(counts, () => nextTick(() => glide(true)))
onMounted(() => {
  nextTick(() => glide(true))
  addEventListener('resize', onResize)
  void document.fonts?.ready.then(() => glide(true))
})
onUnmounted(() => removeEventListener('resize', onResize))

const newOpen = ref(false)

function dot(c: Client) {
  if (c.status === 'frozen') return null
  const t = traffic(c.id)
  if (t?.online) return { cls: 'on', title: `В сети · за 5 мин ${sizeRu(t.recentBytes)}` }
  return {
    cls: '',
    color: expiring(c) !== null ? 'var(--amber)' : 'var(--t4)',
    title: 'Не в сети · за 5 мин нет трафика',
  }
}
</script>

<template>
  <main class="tab on clp">
    <div class="head">
      <div>
        <h1>Клиенты и ключи</h1>
        <p>{{ subtitle }}</p>
      </div>
      <div class="ha">
        <label class="inp">
          <svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" /></svg>
          <input v-model="search" placeholder="Поиск..." aria-label="Поиск клиентов">
        </label>
        <button type="button" class="pill w" @click="newOpen = true">
          <svg class="i" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>Новый клиент
        </button>
      </div>
    </div>

    <div ref="seg" class="seg gl">
      <button type="button" :class="{ on: filter === 'all' }" @click="filter = 'all'">
        Все <b>{{ counts.all }}</b>
      </button>
      <button type="button" :class="{ on: filter === 'exp' }" @click="filter = 'exp'">
        Истекают <b>{{ counts.exp }}</b>
      </button>
      <button type="button" :class="{ on: filter === 'frz' }" @click="filter = 'frz'">
        Заморожены <b>{{ counts.frz }}</b>
      </button>
      <span ref="glideEl" class="glide now" />
    </div>

    <section class="cgrid">
      <div class="ccol">
        <div class="card clist">
          <div class="thead">
            <span>Клиент</span><span>Устройства</span><span>Сегодня</span><span>Выход</span><span>Доступ до</span>
          </div>
          <div ref="rowsEl" class="crows scroll">
            <div v-if="!clients.length" class="more">
              Пока нет клиентов
            </div>
            <div v-else-if="!rows.length" class="more">
              Ничего не найдено
            </div>
            <div
              v-for="c in rows"
              v-else
              :key="c.id"
              class="crow"
              :class="{ on: c.id === selectedId }"
              role="button"
              tabindex="0"
              :aria-pressed="c.id === selectedId"
              data-testid="client-row"
              @click="select(c.id)"
              @keydown.enter.prevent="select(c.id)"
              @keydown.space.prevent="select(c.id)"
            >
              <div class="who">
                <span class="av" :title="dot(c)?.title">{{ initials(c.name) }}<i
                  v-if="dot(c)"
                  :class="dot(c)!.cls"
                  :style="dot(c)!.color ? { background: dot(c)!.color } : undefined"
                  data-testid="client-online"
                /></span>
                <div>
                  <b><span>{{ c.name }}</span><span v-if="c.filterTraffic" title="Фильтрация рекламы и трекеров включена"><svg class="i bi2" viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 4.4-3.4 8.2-8 9-4.6-.8-8-4.6-8-9V6z" /></svg></span><span v-if="c.tgLinked" title="Привязан к Telegram"><svg class="i bi2" viewBox="0 0 24 24"><path d="M21 4L3 11l6 2.4M21 4l-3 16-6.5-4.7M21 4L9 13.4V19l2.6-3.2" /></svg></span></b>
                  <small>{{ c.tgUsername ? `@${c.tgUsername}` : c.tgLinked ? 'Telegram' : 'без Telegram' }}</small>
                </div>
              </div>
              <span>{{ c.deviceCount }}/{{ c.deviceLimit ?? '∞' }}</span>
              <span
                class="t1"
                :title="(traffic(c.id)?.dayRx ?? 0) + (traffic(c.id)?.dayTx ?? 0) > 0 ? `↓ ${sizeRu(traffic(c.id)?.dayRx)} ↑ ${sizeRu(traffic(c.id)?.dayTx)}` : undefined"
              >{{ sizeRu((traffic(c.id)?.dayRx ?? 0) + (traffic(c.id)?.dayTx ?? 0)) }}<small>всего {{ sizeRu((traffic(c.id)?.rxBytes ?? c.rxTotal) + (traffic(c.id)?.txBytes ?? c.txTotal)) }}</small></span>
              <span>{{ c.preferredExit ? c.preferredExit.toUpperCase() : 'Авто' }}</span>
              <span>
                <span v-if="c.status === 'frozen'" class="tag">Заморожен</span>
                <span v-else-if="expiring(c) !== null" class="tag amb">{{ inDaysRu(expiring(c)!) }}</span>
                <template v-else>{{ expiryLabel(c.expiresAt) }}</template>
              </span>
            </div>
          </div>
        </div>
        <OverviewBroadcast />
      </div>
      <div class="card cdet lit" data-testid="client-detail">
        <ClientsDetail
          v-if="selectedId !== null"
          :client-id="selectedId"
          :traffic="traffic(selectedId)"
          :exits="exitTags"
          @changed="refreshClients"
          @deleted="onDeleted"
          @gone="onGone"
        />
        <div v-else class="more">
          Выбери клиента в списке
        </div>
      </div>
    </section>

    <ClientsNewClient v-model:open="newOpen" @created="onCreated" />
  </main>
</template>
