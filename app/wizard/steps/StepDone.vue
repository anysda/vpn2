<script setup lang="ts">
// Экран 8: итог по всем системам и ссылка на панель. Предупреждение (самоподписанный сертификат,
// пропущенный бот) - жёлтое, не ошибка. Панель открывает только кнопка.
import { computed, onMounted, ref } from 'vue'
import { call, refusal, type Status } from '../api'

const status = ref<Status | null>(null)
const error = ref('')
onMounted(async () => {
  try {
    status.value = await call<Status>('GET', '/api/status')
  }
  catch (e) {
    error.value = refusal(e)
  }
})

const COLOR = { ok: 'var(--green)', warn: 'var(--amber)', fail: 'var(--red)' }
const failed = computed(() => status.value?.systems.some(s => s.state === 'fail'))
</script>

<template>
  <section>
    <p v-if="error" role="alert" class="wzbad">{{ error }}</p>
    <p v-else-if="!status" class="sub2" role="status">Собираю итог…</p>
    <template v-else>
      <p class="mt">{{ failed ? 'Установка закончилась, но не всё работает: что не так - в строках ниже.' : 'Установка закончилась, всё работает.' }}</p>
      <div v-for="s in status.systems" :key="s.name" class="fld wzsys" data-testid="system">
        <span><i class="dot" :style="{ background: COLOR[s.state] }" />{{ s.name }}</span>
        <code :class="{ wzwarn: s.state === 'warn', wzbad: s.state === 'fail' }">{{ s.detail }}</code>
      </div>
      <div class="mf">
        <a class="pill w" :href="status.panel_url" rel="noopener">Открыть панель</a>
      </div>
      <p class="sub2 wzfoot">Ссылка мастера через несколько минут перестанет действовать: дальше всё в панели.</p>
    </template>
  </section>
</template>
