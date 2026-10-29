<script setup lang="ts">
import '~/assets/css/clients.css'
import { pluralRu } from '#shared/plural'
import { expiryLabel, useClients } from '~/composables/useClients'
import type { Client, ClientTraffic } from '~/composables/useClients'
import type { ClientDot } from '~/components/Panel/ClientRow.vue'
import { useExitTags } from '~/composables/useRoutes'
import { anim, expiringInDays, inDaysRu, initials, sizeRu } from '~/utils/clients-view'

useHead({ title: 'Клиенты — anysda-vpn2' })

const { clients: fetched, refresh: refreshClients, status: clientsStatus } = useClients()
// Удалённые клиенты, которых ещё может вернуть ответ списка, начатый до удаления:
// их не показывать и не выбирать снова, иначе карточка запросит удалённого и получит 404.
const removed = ref<number[]>([])
const clients = computed(() => fetched.value.filter(c => !removed.value.includes(c.id)))
watch(fetched, (list) => {
  if (removed.value.some(id => !list.some(c => c.id === id))) removed.value = removed.value.filter(id => list.some(c => c.id === id))
})
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
definePageMeta({ title: 'Клиенты и ключи' })
usePageSub(subtitle)

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

/** Клиент виден в списке при текущих фильтре и поиске. */
function visible(c: Client) {
  const q = search.value.trim().toLowerCase()
  return (filter.value === 'all' || (filter.value === 'exp' ? expiring(c) !== null : c.status === 'frozen'))
    && (!q || `${c.name}${c.tgUsername ? `@${c.tgUsername}` : ''}`.toLowerCase().includes(q))
}
const rows = computed(() => sorted.value.filter(visible))

const route = useRoute()
const router = useRouter()
// Телефон: список и карточка клиента — два экрана. Открытый клиент — ?id= в
// адресе и запись в истории: системный «назад» и «Назад» в шапке ведут к списку,
// ссылка и обновление страницы открывают карточку.
const mobile = useMobile()
const screen = computed(() => (route.query.id ? 'client' : 'list'))
watch(screen, () => { if (mobile.value) scrollTo(0, 0) })
const stack = useBackStack()
/** Адрес раздела с карточкой клиента `id` или без неё — список. */
const urlOf = (id?: number) => {
  const query = { ...route.query, id: id ? String(id) : undefined }
  return router.resolve({ path: route.path, query }).fullPath
}
const toList = () => stack.leave(urlOf())

// Выбранный клиент: из ссылки ?id= (топ на Обзоре), иначе первый в списке.
const selectedId = ref<number | null>(Number(route.query.id) || null)
watch(() => route.query.id, (id) => { if (Number(id)) selectedId.value = Number(id) })
/**
 * Кого выбрать вместо пропавшего `id`: того, кто стоял под ним в прежнем списке
 * и остался в новом, а если ниже никого - того, кто стоял над ним.
 */
function neighbor(id: number | null, before: readonly Client[], now: readonly Client[]) {
  const at = before.findIndex(c => c.id === id)
  const left = (c: Client) => now.some(n => n.id === c.id)
  const near = at < 0 ? undefined : before.slice(at + 1).find(left) ?? before.slice(0, at).reverse().find(left)
  return (near ?? now[0])?.id ?? null
}
// Единственное место, где выбор уходит с пропавшего клиента: удалён здесь (список
// без него мог прийти раньше подтверждения удаления), другим админом или впервые
// загружен список.
watch(sorted, (list, before) => {
  // Список грузится только в браузере: до первого ответа ?id= из ссылки не сбрасывать.
  if (!list.length && clientsStatus.value !== 'success' && clientsStatus.value !== 'error') return
  if (selectedId.value !== null && list.some(c => c.id === selectedId.value)) return
  // Сосед - из того, что админ видит слева: скрытого поиском или фильтром не открывать.
  selectedId.value = neighbor(selectedId.value, (before ?? []).filter(visible), list.filter(visible))
  // Открытый на телефоне клиент пропал из списка (удалён, в том числе последний, или
  // ушёл из фильтра): карточка с ним уже снята, и её «удалён» до страницы не дойдёт.
  if (mobile.value && screen.value === 'client') void toList()
}, { immediate: true })

const headerBack = useHeaderBack()
watchEffect(() => {
  headerBack.value = { path: route.path, to: mobile.value && screen.value === 'client' ? urlOf() : '' }
})
onUnmounted(() => { headerBack.value = { path: '', to: '' } })

function select(id: number) {
  selectedId.value = id
  if (mobile.value) void stack.push(urlOf(id))
}

async function onCreated(id: number) {
  await refreshClients()
  selectedId.value = id
}

// Клиент удалён здесь или другим админом (карточка получила 404): убрать его
// из списка, без тоста; соседа выберет наблюдатель списка выше.
async function onRemoved(id: number) {
  if (!removed.value.includes(id)) removed.value = [...removed.value, id]
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

function dot(c: Client): ClientDot | null {
  if (c.status === 'frozen') return null
  const t = traffic(c.id)
  if (t?.online) return { cls: 'on', title: `В сети · за 5 мин ${sizeRu(t.recentBytes)}` }
  return {
    cls: '',
    color: expiring(c) !== null ? 'var(--amber)' : 'var(--t4)',
    title: 'Не в сети · за 5 мин нет трафика',
  }
}

const today = (c: Client) => sizeRu((traffic(c.id)?.dayRx ?? 0) + (traffic(c.id)?.dayTx ?? 0))
const tgText = (c: Client) => (c.tgUsername ? `@${c.tgUsername}` : c.tgLinked ? 'Telegram' : 'без Telegram')
/** Мета строки на телефоне: «3/3 · NL · @nick». */
const meta = (c: Client) => `${c.deviceCount}/${c.deviceLimit ?? '∞'} · ${c.preferredExit ? c.preferredExit.toUpperCase() : 'Авто'} · ${tgText(c)}`
</script>

<template>
  <main class="tab on clp" :class="mobile ? `m-${screen}` : undefined">
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
            <PanelClientRow
              v-for="c in rows"
              v-else
              :key="c.id"
              :name="c.name"
              :initials="initials(c.name)"
              :selected="c.id === selectedId"
              :mobile="mobile"
              :dot="dot(c)"
              @select="select(c.id)"
            >
              <template #icons><span v-if="c.filterTraffic" title="Фильтрация рекламы и трекеров включена"><svg class="i bi2" viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 4.4-3.4 8.2-8 9-4.6-.8-8-4.6-8-9V6z" /></svg></span><span v-if="c.tgLinked" title="Привязан к Telegram"><svg class="i bi2" viewBox="0 0 24 24"><path d="M21 4L3 11l6 2.4M21 4l-3 16-6.5-4.7M21 4L9 13.4V19l2.6-3.2" /></svg></span></template>
              <template #meta>{{ mobile ? meta(c) : tgText(c) }}</template>
              <div v-if="mobile" class="crt">
                <span class="t1">{{ today(c) }}</span>
                <span v-if="c.status === 'frozen'" class="tag access-tag">{{ c.accessStatus }}</span>
                <span v-else-if="expiring(c) !== null" class="tag amb">{{ inDaysRu(expiring(c)!) }}</span>
                <small v-else>{{ c.expiresAt ? expiryLabel(c.expiresAt) : 'бессрочно' }}</small>
              </div>
              <template v-else>
                <span>{{ c.deviceCount }}/{{ c.deviceLimit ?? '∞' }}</span>
                <span
                  class="t1"
                  :title="(traffic(c.id)?.dayRx ?? 0) + (traffic(c.id)?.dayTx ?? 0) > 0 ? `↓ ${sizeRu(traffic(c.id)?.dayRx)} ↑ ${sizeRu(traffic(c.id)?.dayTx)}` : undefined"
                >{{ today(c) }}<small>всего {{ sizeRu((traffic(c.id)?.rxBytes ?? c.rxTotal) + (traffic(c.id)?.txBytes ?? c.txTotal)) }}</small></span>
                <span>{{ c.preferredExit ? c.preferredExit.toUpperCase() : 'Авто' }}</span>
                <span>
                  <span v-if="c.status === 'frozen'" class="tag access-tag">{{ c.accessStatus }}</span>
                  <span v-else-if="expiring(c) !== null" class="tag amb">{{ inDaysRu(expiring(c)!) }}</span>
                  <template v-else>{{ expiryLabel(c.expiresAt) }}</template>
                </span>
              </template>
            </PanelClientRow>
          </div>
        </div>
        <OverviewBroadcast />
      </div>
      <div class="card cdet lit" data-testid="client-detail">
        <ClientsDetail
          v-if="selectedId !== null && (!mobile || screen === 'client')"
          :client-id="selectedId"
          :traffic="traffic(selectedId)"
          :exits="exitTags"
          @changed="refreshClients"
          @deleted="onRemoved"
          @gone="onRemoved"
        />
        <div v-else class="more">
          Выбери клиента в списке
        </div>
      </div>
    </section>

    <ClientsNewClient v-model:open="newOpen" @created="onCreated" />
  </main>
</template>
