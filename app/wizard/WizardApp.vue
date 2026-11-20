<script setup lang="ts">
// Мастер установки: одна карточка, шаги сверху. Всё состояние - на сервере: страница читает его
// при открытии и открывает нужный экран; назад/вперёд браузера ходят по шагам черновика.
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { call, exitsReady, expired, offlineSince, putDraft, readState, refusal, start, type Patch, type State } from './api'
import StepExits from './steps/StepExits.vue'
import StepPanel from './steps/StepPanel.vue'
import StepTelegram from './steps/StepTelegram.vue'
import StepBackup from './steps/StepBackup.vue'
import StepYoutube from './steps/StepYoutube.vue'
import StepSummary from './steps/StepSummary.vue'
import StepInstall from './steps/StepInstall.vue'
import StepDone from './steps/StepDone.vue'

const TITLES = ['Экзиты', 'Панель', 'Telegram', 'Бэкапы', 'YouTube', 'Проверка', 'Установка', 'Готово']
const HEADS = ['Экзиты и ключи', 'Панель', 'Telegram-бот', 'Бэкапы', 'YouTube через РФ', 'Проверьте и установите', 'Установка', 'Готово']
const FORMS = [StepExits, StepPanel, StepTelegram, StepBackup, StepYoutube, StepSummary]

const state = ref<State | null>(null)
const screen = ref(0)
const form = ref<{ ready: boolean, collect: () => Patch } | null>(null)
const busy = ref(false)
const error = ref('')
const now = ref(Date.now())
const lost = computed(() => offlineSince.value !== null && now.value - offlineSince.value >= 10_000)

/** Дальше этого экрана черновик не пускает: на нём ещё не всё, без чего сервер не начнёт установку. */
function firstIncomplete({ draft: d, secrets: s, backup_key }: State) {
  if (!d.admin_ssh_key.trim() || !exitsReady(d.exits)) return 1
  if (!d.panel.admin_user || !s.admin_password) return 2
  if (d.telegram.enabled && (!d.telegram.chat_id || !s.telegram_bot_token)) return 3
  const s3 = d.backup.s3
  if (backup_key !== 'seen' || (d.backup.target === 's3' && !(s3.endpoint && s3.bucket && s.s3_access_key && s.s3_secret_key))) return 4
  return 6
}
const reachable = computed(() => state.value?.phase === 'filling' ? firstIncomplete(state.value) : 0)

function show(n: number, push = true) {
  if (n === screen.value) return
  if (push) history.pushState({ screen: n }, '')
  else history.replaceState({ screen: n }, '')
  screen.value = n
  error.value = ''
  window.scrollTo(0, 0)
}

async function load() {
  const next = await readState().catch((e: unknown) => void refusal(e))
  if (!next) return
  const first = !screen.value
  state.value = next
  const wanted = first ? next.draft.step : screen.value
  if (next.phase !== 'filling') show(next.phase === 'installing' ? 7 : 8, !first)
  else show(Math.min(Math.max(wanted, 1), 6, firstIncomplete(next)), !first)
}

function go(n: number) {
  show(n)
  putDraft({ step: n }).catch(() => {})
}

async function next() {
  const { patch, secrets, then } = form.value!.collect()
  busy.value = true
  error.value = ''
  try {
    if (screen.value === 6) await call('POST', '/api/install')
    else {
      for (const [name, value] of Object.entries(secrets ?? {})) await call('PUT', `/api/secret/${name}`, { value })
      await putDraft({ ...patch, step: screen.value + 1 })
      await then?.()
    }
    const done = screen.value
    await load()
    if (screen.value === done && done < 6) show(done + 1)
  }
  catch (e) {
    error.value = refusal(e)
    await load()
  }
  finally {
    busy.value = false
  }
}

function popped(event: PopStateEvent) {
  const n = Number(event.state?.screen)
  if (state.value?.phase === 'filling' && n >= 1 && n <= reachable.value) screen.value = n
  else history.replaceState({ screen: screen.value }, '')
}
// Вкладку вернули на экран: другая вкладка могла начать установку или поменять черновик.
function visible() {
  if (document.visibilityState === 'visible' && state.value && !expired.value) load()
}

// Новую ссылку вставили в эту же вкладку: смена одного фрагмента страницу не грузит заново.
function relinked() {
  if (location.hash.length > 1) location.reload()
}

let ticker: ReturnType<typeof setInterval> | undefined
onMounted(async () => {
  ticker = setInterval(() => (now.value = Date.now()), 1000)
  addEventListener('popstate', popped)
  addEventListener('hashchange', relinked)
  document.addEventListener('visibilitychange', visible)
  await start().catch(refusal)
  if (!expired.value) await load()
})
onUnmounted(() => {
  clearInterval(ticker)
  removeEventListener('popstate', popped)
  removeEventListener('hashchange', relinked)
  document.removeEventListener('visibilitychange', visible)
})
</script>

<template>
  <div>
    <div class="light" />
    <div class="wrap wzw">
      <main class="bare">
        <div v-if="expired" class="card wz wzend" data-testid="expired">
          <div class="logo"><span>anysda-vpn2</span></div>
          <h1>Ссылка устарела</h1>
          <p class="mt">
            Запустите <code>./deploy.sh</code> ещё раз: он напечатает новую ссылку. Всё, что вы уже ввели, сохранено на сервере -
            новая ссылка откроет мастер с того же места.
          </p>
        </div>
        <div v-else-if="state && screen" class="card wz">
          <div class="logo"><span>anysda-vpn2</span><small>установка</small></div>
          <ol class="wzbar" aria-label="Шаги установки">
            <li v-for="(title, i) in TITLES" :key="title" :class="{ on: i + 1 === screen, done: i + 1 < screen }">
              <button type="button" :disabled="i + 1 === screen || i + 1 > reachable" :aria-current="i + 1 === screen ? 'step' : undefined" @click="go(i + 1)">
                <i>{{ i + 1 }}</i><span>{{ title }}</span>
              </button>
            </li>
          </ol>
          <h1>{{ HEADS[screen - 1] }}</h1>
          <component :is="FORMS[screen - 1]" v-if="screen <= 6" ref="form" :key="screen" :state="state" @go="go" />
          <StepInstall v-else-if="screen === 7" :state="state" @reload="load" />
          <StepDone v-else />
          <template v-if="screen <= 6">
            <div v-if="error" class="al red" role="alert">{{ error }}</div>
            <div class="mf">
              <button v-if="screen > 1" type="button" class="pill" :disabled="busy" @click="go(screen - 1)">Назад</button>
              <button type="button" class="pill w" :disabled="!form?.ready || busy" @click="next">{{ screen === 6 ? 'Установить' : 'Далее' }}</button>
            </div>
          </template>
        </div>
        <p v-if="lost" class="wzlost" role="status">связь восстанавливается…</p>
      </main>
    </div>
  </div>
</template>
