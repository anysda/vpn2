<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import BackButton from '../app/components/Panel/BackButton.vue'
import BroadcastForm from '../app/components/Panel/BroadcastForm.vue'
import ClientRow from '../app/components/Panel/ClientRow.vue'
import type { ClientDot } from '../app/components/Panel/ClientRow.vue'
import ClientHeading from '../app/components/Panel/ClientHeading.vue'
import DeviceName from '../app/components/Panel/DeviceName.vue'
import { initials } from '../app/utils/clients-view'

type Connection = 'connected' | 'seen' | 'never' | 'unknown'
interface Device { id: number, name: string, ready: boolean }
interface Client { id: number, name: string, telegram: boolean, allowed: boolean, status: Connection, devices: Device[] }
interface State {
  clients: Client[], exit_name: string, generation: number, leader_changed: boolean, autonomous: boolean, cancelled: boolean
  remaining: number | null, deadline: number | null, snapshot_age: number | null, bot_ready: boolean
  delivery: { sent: number, errors: number, pending: number, without_telegram: number, denied: number }
}
const state = ref<State | null>(null)
const password = ref('')
const error = ref('')
const busy = ref(false)
const search = ref('')
const selectedId = ref<number | null>(null)
const screen = ref(location.hash ? 'client' : 'list')
const mobile = ref(matchMedia('(max-width:720px)').matches)
const message = ref('')
const sending = ref(false)
const result = ref('')
const profile = ref<{ id: number, uri: string } | null>(null)
const now = ref(Date.now())
const received = ref(Date.now())
const statusText: Record<Connection, string> = { connected: 'Подключён', seen: 'Подключался', never: 'Ещё не подключался', unknown: 'Статус неизвестен' }
const dot = (c: Client): ClientDot => c.status === 'connected' ? { cls: 'on', title: statusText[c.status] } : { cls: '', color: 'var(--t4)', title: statusText[c.status] }
const clients = computed(() => state.value?.clients.filter(c => c.name.toLowerCase().includes(search.value.toLowerCase())) ?? [])
const selected = computed(() => state.value?.clients.find(c => c.id === selectedId.value))
// Таймер тикает раз в секунду, а ответ приходит между тиками: now бывает меньше received.
const elapsed = computed(() => Math.max(0, now.value - received.value) / 1000)
const remaining = computed(() => state.value?.remaining == null ? null : Math.max(0, state.value.remaining - Math.floor(elapsed.value)))
const snapshotAge = computed(() => state.value?.snapshot_age == null ? 'неизвестен' : `${Math.floor(Math.max(0, state.value.snapshot_age + elapsed.value) / 60)} мин`)
const status = computed(() => state.value?.cancelled ? 'Основное подключение снова недоступно. Аварийный доступ продолжает работать' : remaining.value !== null ? `Основной VPN восстановлен. До отключения аварийного доступа: ${Math.floor(remaining.value / 60)}:${String(remaining.value % 60).padStart(2, '0')}` : 'Аварийный доступ готов. Конфиги и ссылка на инструкцию отправляются автоматически')

async function api(path: string, body?: object) {
  const response = await fetch(path, { redirect: 'manual', ...(body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}) })
  if (response.type === 'opaqueredirect') { location.reload(); throw new Error('Переход на нового лидера') }
  if (!response.ok) {
    if (response.status === 401 || response.status === 503) { state.value = null; profile.value = null }
    throw new Error(response.status === 429 ? 'Предел попыток входа. Повторите через 5 минут.' : response.status === 401 ? 'Неверный пароль или сессия завершена.' : 'Не удалось получить ответ. Повторите попытку.')
  }
  return response.json()
}
async function refresh() {
  const previous = state.value?.generation
  state.value = await api('/api/state') as State
  received.value = Date.now()
  if (previous !== state.value.generation) profile.value = null
  if (!state.value.clients.some(c => c.id === selectedId.value)) selectedId.value = state.value.clients[0]?.id ?? null
  if (profile.value && !selected.value?.devices.some(d => d.id === profile.value?.id && d.ready)) profile.value = null
}
async function login() {
  busy.value = true; error.value = ''
  try { await api('/api/login', { password: password.value }); password.value = ''; await refresh() }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function logout() {
  try { await api('/api/logout', {}) }
  catch (e) { error.value = (e as Error).message }
  state.value = null; profile.value = null
}
async function send() {
  sending.value = true
  try { result.value = (await api('/api/broadcast', { text: message.value })).message; message.value = '' }
  catch (e) { result.value = (e as Error).message }
  finally { sending.value = false }
}
async function openProfile(id: number) {
  profile.value = null
  try { const data = await api(`/api/profile/${id}/uri`); if (selected.value?.devices.some(d => d.id === id)) profile.value = { id, uri: data.uri } }
  catch (e) { error.value = (e as Error).message }
}
async function copy() {
  try { await navigator.clipboard.writeText(profile.value!.uri); result.value = 'Ссылка скопирована' }
  catch { result.value = 'Выделите и скопируйте ссылку вручную' }
}
function select(id: number) { selectedId.value = id; profile.value = null; if (mobile.value) location.hash = String(id) }
function back() { location.hash = '' }
function hashChanged() { screen.value = location.hash ? 'client' : 'list'; if (location.hash) selectedId.value = Number(location.hash.slice(1)); scrollTo(0, 0) }
const header = ref<HTMLElement>()
let observer: ResizeObserver | undefined
let timer: ReturnType<typeof setInterval>
let polling = false
function resize() { mobile.value = matchMedia('(max-width:720px)').matches }
onMounted(async () => {
  addEventListener('hashchange', hashChanged); addEventListener('resize', resize)
  try { await refresh() } catch { /* First request shows the password form. */ }
  await nextTick()
  observer = new ResizeObserver(() => document.documentElement.style.setProperty('--mh', `${header.value?.offsetHeight ?? 66}px`))
  if (header.value) observer.observe(header.value)
  timer = setInterval(async () => {
    now.value = Date.now()
    if (state.value && !polling && now.value - received.value >= 10000) {
      polling = true
      try { await refresh(); error.value = '' }
      catch (e) { state.value = null; profile.value = null; error.value = (e as Error).message }
      finally { polling = false }
    }
  }, 1000)
})
onUnmounted(() => { clearInterval(timer); observer?.disconnect(); removeEventListener('hashchange', hashChanged); removeEventListener('resize', resize) })
</script>

<template>
  <div class="light" />
  <div v-if="!state" class="wrap lgw">
    <main class="tab on bare login"><div class="card lgn">
      <div class="logo" style="justify-content:center"><span>anysda-vpn2</span></div>
      <p class="lgsub">Аварийный режим</p>
      <form @submit.prevent="login">
        <label class="fl2"><span>Пароль</span><input v-model="password" class="in" type="password" required autocomplete="current-password" :disabled="busy"></label>
        <div v-if="error" class="al red" role="alert">{{ error }}</div>
        <div class="mf"><button class="pill w lgo" type="submit" :disabled="busy">Войти</button></div>
      </form>
    </div></main>
  </div>
  <div v-else class="wrap">
    <header ref="header">
      <BackButton v-if="mobile && screen === 'client'" @back="back" />
      <div class="logo"><span>anysda-vpn2</span></div>
      <div v-if="mobile" class="mhead"><h1>Аварийный режим</h1><p>{{ state.exit_name }}</p></div>
      <nav class="tabs"><a class="on" href="/" aria-current="page">Аварийный режим</a></nav>
      <div class="hr"><span v-if="!mobile" class="exn">{{ state.exit_name }}</span><button type="button" class="pill" @click="logout">Выйти</button></div>
    </header>
    <main class="tab on clp" :class="mobile ? `m-${screen}` : undefined">
      <div class="card emergency-state" role="status">
        <p>{{ status }}</p>
        <p v-if="state.deadline">Отключение: {{ new Date(state.deadline * 1000).toLocaleString('ru-RU', { timeZoneName: 'short' }) }}</p>
        <p v-if="state.leader_changed">Лидер сменился: {{ state.exit_name }}. Используйте новые реквизиты; администратору отправляются новый адрес и пароль.</p>
        <p v-if="state.autonomous">Разделение сети: согласование между экзитами нарушено. Этот exit работает автономно.</p>
        <p>Отправлено: {{ state.delivery.sent }} · Ошибок: {{ state.delivery.errors }} · В очереди: {{ state.delivery.pending }} · Без Telegram: {{ state.delivery.without_telegram }} · Без доступа: {{ state.delivery.denied }}</p>
        <p>Возраст снимка: {{ snapshotAge }}</p>
      </div>
      <div v-if="error" class="al red" role="alert">{{ error }}</div>
      <div class="head"><div><h1>Клиенты</h1><p>{{ state.clients.length }} · {{ state.exit_name }}</p></div><div class="ha"><label class="inp"><svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" /></svg><input v-model="search" placeholder="Поиск..." aria-label="Поиск клиентов"></label></div></div>
      <section class="cgrid">
        <div class="ccol">
          <div class="card clist">
            <div class="thead"><span>Клиент</span><span>Устройства</span><span>Подключение</span></div>
            <div class="crows scroll">
              <div v-if="!clients.length" class="more">Ничего не найдено</div>
              <ClientRow v-for="c in clients" v-else :key="c.id" :name="c.name" :initials="initials(c.name)" :selected="c.id === selectedId" :mobile="mobile" :dot="dot(c)" @select="select(c.id)">
                <template #meta>{{ mobile ? `${c.devices.length} · ${c.telegram ? 'Telegram' : 'без Telegram'}` : c.telegram ? 'Telegram' : 'без Telegram' }}</template>
                <div v-if="mobile" class="crt"><small>{{ statusText[c.status] }}</small></div>
                <template v-else><span>{{ c.devices.length }}</span><span>{{ statusText[c.status] }}</span></template>
              </ClientRow>
            </div>
          </div>
          <BroadcastForm v-model="message" :configured="state.bot_ready" :sending="sending" :result="result" button-label="Отправить сообщение" @send="send" />
        </div>
        <div class="card cdet lit" data-testid="client-detail">
          <div v-if="selected" class="cdet-in">
            <ClientHeading :initials="initials(selected.name)" :subtitle="selected.telegram ? 'Привязан к Telegram' : 'Telegram не привязан'" :status="statusText[selected.status]" :status-class="selected.status === 'connected' ? 'ok' : ''"><h2>{{ selected.name }}</h2></ClientHeading>
            <p v-if="!selected.telegram" class="al">Нет Telegram. Материалы доступны ниже для ручной передачи.</p>
            <p v-if="!selected.allowed" class="al">Доступ запрещён или срок истёк. Профили недоступны.</p>
            <div class="sub"><h3>Девайсы {{ selected.devices.length }}</h3></div>
            <div class="devl scroll">
              <div v-for="dev in selected.devices" :key="dev.id" class="dev" data-testid="device-row">
                <DeviceName :name="dev.name" />
                <div class="kb" :class="{ dk3: mobile }"><button type="button" :disabled="!dev.ready" @click="openProfile(dev.id)">Hysteria2</button></div>
                <div v-if="profile?.id === dev.id" class="profile clm">
                  <div class="kvr"><span>Ссылка</span><code>{{ profile.uri }}</code></div>
                  <img class="qr" :src="`/api/profile/${dev.id}/qr`" alt="QR профиля Hysteria2">
                  <div class="mf"><button type="button" class="pill" @click="copy">Копировать</button><a class="pill w" :href="`/api/profile/${dev.id}/file`" download>Скачать</a></div>
                </div>
              </div>
            </div>
          </div><div v-else class="more">Выбери клиента в списке</div>
        </div>
      </section>
    </main>
  </div>
</template>
