<script setup lang="ts">
import '~/assets/css/clients.css'
import { inDaysRu, plural } from '#shared/plural'
import { parseOutbound } from '#shared/outbounds'
import type { Client } from '~/composables/useClients'

useHead({ title: 'Клиенты — anysda-vpn2' })
definePageMeta({ title: 'Клиенты и ключи' })

const route = useRoute()
const router = useRouter()
const mobile = useMobile()
const { clients: fetched, refresh, status, error } = useClients()
// Удалённые, которых ещё может вернуть ответ списка, начатый до удаления.
const removed = ref<number[]>([])
const clients = computed(() => fetched.value.filter(c => !removed.value.includes(c.id)))

const { data: traffic, refresh: refreshTraffic } = useFetch('/api/clients/traffic', { default: () => ({}) as Api<'/api/clients/traffic'>, server: false })
usePoll(refreshTraffic, 3000)
const { data: outbounds } = useFetch('/api/routes/outbounds', { default: () => [], server: false })
const exits = computed(() => [...new Set(outbounds.value.map(o => parseOutbound(o.name))
  .filter(p => p?.variant === 'direct' && p.tag !== 'ru').map(p => p!.tag))].sort())

// До первого ответа на этом открытии «Пока нет клиентов» и «0 клиентов» были бы неправдой.
const listed = ref(false)
const loadError = ref('')
watch(status, (s) => {
  if (s === 'success') [listed.value, loadError.value] = [true, '']
  const code = (error.value as { statusCode?: number } | null)?.statusCode
  if (s === 'error') loadError.value = code ? `ошибка сервера (код ${code})` : /timeout/i.test(String(error.value)) ? 'сервер не ответил вовремя' : 'нет связи с сервером'
}, { flush: 'sync' })

const subtitle = computed(() => {
  const n = clients.value.length, d = clients.value.reduce((s, c) => s + c.deviceCount, 0)
  return listed.value ? `${n} ${plural(n, 'client')} · ${d} ${plural(d, 'device')}` : ''
})
usePageSub(() => subtitle.value)

type Filter = 'all' | 'exp' | 'frz'
const FILTERS = [['all', 'Все'], ['exp', 'Истекают'], ['frz', 'Заморожены']] as const
const filter = computed<Filter>(() => route.query.f === 'exp' || route.query.f === 'frz' ? route.query.f : 'all')
const filterTo = (f: Filter) => ({ query: { ...route.query, f: f === 'all' ? undefined : f } })
const search = ref('')
const t = (id: number) => traffic.value[id] ?? null
const expiring = (c: Client) => expiringInDays(c.expiresAt, c.status)
const counts = computed(() => ({
  all: clients.value.length,
  exp: clients.value.filter(c => expiring(c) !== null).length,
  frz: clients.value.filter(c => c.status === 'frozen').length,
}))

function visible(c: Client) {
  const q = search.value.trim().toLowerCase()
  const inFilter = filter.value === 'all' || (filter.value === 'exp' ? expiring(c) !== null : c.status === 'frozen')
  return inFilter && (!q || `${c.name}${c.tgUsername ? `@${c.tgUsername}` : ''}`.toLowerCase().includes(q))
}
// Сверху - кто в сети (трафик за 5 мин), дальше по объёму за сутки, затем по имени.
const rows = computed(() => {
  const day = (id: number) => (t(id)?.dayRx ?? 0) + (t(id)?.dayTx ?? 0)
  return clients.value.filter(visible).sort((a, b) => Number(t(b.id)?.online ?? false) - Number(t(a.id)?.online ?? false)
    || day(b.id) - day(a.id) || a.name.localeCompare(b.name, 'ru'))
})

// Выбранный клиент - ?id= в адресе (ссылка с Обзора, «назад» телефона), иначе первый в списке.
const selectedId = ref<number | null>(Number(route.query.id) || null)
watch(() => route.query.id, id => Number(id) && (selectedId.value = Number(id)))
const screen = computed(() => route.query.id ? 'client' : 'list')
function select(id: number) {
  selectedId.value = id
  if (mobile.value) void router.push({ query: { ...route.query, id } })
}
// Пропавшего (удалён, скрыт поиском) сменяет тот, кто стоял на его месте; телефон - к списку.
let lastIndex = 0
function selectNeighbour(list: Client[]) {
  selectedId.value = list[Math.min(lastIndex, list.length - 1)]?.id ?? null
  if (mobile.value && screen.value === 'client') void router.replace({ query: { ...route.query, id: undefined } })
}
watch(rows, (list) => {
  const at = list.findIndex(c => c.id === selectedId.value)
  if (at >= 0) lastIndex = at
  else if (listed.value) selectNeighbour(list)
})

async function onCreated(id: number) {
  await refresh()
  const c = clients.value.find(x => x.id === id)
  if (c && !visible(c)) {
    search.value = ''
    await router.replace(filterTo('all'))
  }
  selectedId.value = id
}
async function onGone(id: number) {
  removed.value = [...removed.value, id]
  await refresh()
}
const onDeleting = (id: number) => selectNeighbour(rows.value.filter(c => c.id !== id))

const newOpen = ref(false)
function dot(c: Client) {
  if (c.status === 'frozen') return null
  const tr = t(c.id)
  if (tr?.online) return { cls: 'on', title: `В сети · за 5 мин ${sizeBinRu(tr.recentBytes)}` }
  return { cls: '', color: expiring(c) !== null ? 'var(--amber)' : 'var(--t4)', title: 'Не в сети · за 5 мин нет трафика' }
}
const usage = (c: Client) => {
  const [rx, tx] = [t(c.id)?.dayRx ?? 0, t(c.id)?.dayTx ?? 0]
  return {
    day: sizeBinRu(rx + tx),
    split: rx + tx > 0 ? `↓ ${sizeBinRu(rx)} ↑ ${sizeBinRu(tx)}` : undefined,
    total: sizeBinRu((t(c.id)?.rxBytes ?? c.rxTotal) + (t(c.id)?.txBytes ?? c.txTotal)),
  }
}
const exitText = (c: Client) => c.preferredExit ? c.preferredExit.toUpperCase() : 'Авто'
const tgText = (c: Client) => c.tgUsername ? `@${c.tgUsername}` : c.tgLinked ? 'Telegram' : 'без Telegram'
</script>

<template>
  <main class="tab on clp" :class="`m-${screen}`">
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

    <div class="seg">
      <NuxtLink v-for="[f, label] in FILTERS" :key="f" :to="filterTo(f)" replace role="button" :aria-pressed="filter === f" :class="{ on: filter === f }">
        {{ label }} <b>{{ counts[f] }}</b>
      </NuxtLink>
    </div>

    <section class="cgrid">
      <div class="ccol">
        <div class="card clist">
          <div class="thead">
            <span>Клиент</span><span>Устройства</span><span>Сегодня</span><span>Выход</span><span>Доступ до</span>
          </div>
          <div v-if="loadError" role="alert" class="al red">
            <b>{{ listed ? 'Не удалось обновить список клиентов' : 'Не удалось загрузить клиентов' }}</b>
            {{ listed ? `Пока показан список последней загрузки. ${loadError}` : loadError }}
          </div>
          <div class="crows scroll">
            <template v-if="listed">
              <div v-if="!rows.length" class="more">
                {{ clients.length ? 'Ничего не найдено' : 'Пока нет клиентов' }}
              </div>
              <div
                v-for="c in rows" :key="c.id" class="crow" :class="{ on: !mobile && c.id === selectedId }" role="button" tabindex="0"
                :aria-pressed="c.id === selectedId" data-testid="client-row" @click="select(c.id)" @keydown.enter.prevent="select(c.id)" @keydown.space.prevent="select(c.id)">
                <div class="who">
                  <span class="av" :title="dot(c)?.title">
                    {{ initials(c.name) }}
                    <i v-if="dot(c)" :class="dot(c)!.cls" :style="{ background: dot(c)!.color }" data-testid="client-online" />
                  </span>
                  <div>
                    <b>
                      <span>{{ c.name }}</span>
                      <svg v-if="c.filterTraffic" class="i bi2" viewBox="0 0 24 24"><title>Фильтрация рекламы и трекеров включена</title><path d="M12 3l8 3v6c0 4.4-3.4 8.2-8 9-4.6-.8-8-4.6-8-9V6z" /></svg>
                      <svg v-if="c.tgLinked" class="i bi2" viewBox="0 0 24 24"><title>Привязан к Telegram</title><path d="M21 4L3 11l6 2.4M21 4l-3 16-6.5-4.7M21 4L9 13.4V19l2.6-3.2" /></svg>
                    </b>
                    <small>{{ mobile ? `${c.deviceCount}/${c.deviceLimit ?? '∞'} · ${exitText(c)} · ${tgText(c)}` : tgText(c) }}</small>
                  </div>
                </div>
                <span class="cdn">{{ c.deviceCount }}/{{ c.deviceLimit ?? '∞' }}</span>
                <span class="t1" :title="usage(c).split">{{ usage(c).day }}<small>всего {{ usage(c).total }}</small></span>
                <span class="cdn">{{ exitText(c) }}</span>
                <span class="cxp">
                  <span v-if="c.status === 'frozen'" class="tag access-tag">{{ c.accessStatus }}</span>
                  <span v-else-if="expiring(c) !== null" class="tag amb">{{ inDaysRu(expiring(c)!) }}</span>
                  <template v-else>{{ mobile && !c.expiresAt ? 'бессрочно' : expiryLabelLocal(c.expiresAt) }}</template>
                </span>
              </div>
            </template>
          </div>
        </div>
        <OverviewBroadcast />
      </div>
      <div class="card cdet lit" data-testid="client-detail">
        <ClientsDetail
          v-if="selectedId !== null && (!mobile || screen === 'client')" :client-id="selectedId" :traffic="t(selectedId)" :exits="exits"
          @changed="refresh" @deleting="onDeleting" @gone="onGone" />
        <div v-else class="more">Выбери клиента в списке</div>
      </div>
    </section>

    <ClientsNewClient v-model:open="newOpen" @created="onCreated" />
  </main>
</template>
