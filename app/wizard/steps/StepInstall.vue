<script setup lang="ts">
// Экран 7: ход установки - общий экран задания (ui/ExitJob.vue): стадии, журнал текущей,
// «Повторить» и «Продолжить без экзита». Пока задания нет или оно кончилось, а мастер ещё
// не перешёл к итогу, страница перечитывает состояние.
import { onUnmounted, ref } from 'vue'
import ExitJob from '../../ui/ExitJob.vue'
import { transport, type State } from '../api'
import type { ExitJob as Job } from '../../ui/transport'

const props = defineProps<{ state: State }>()
const emit = defineEmits<{ reload: [] }>()

const done = ref(false)
const timer = setInterval(() => {
  if (!props.state.job || done.value) emit('reload')
}, 2000)
onUnmounted(() => clearInterval(timer))

function update(job: Job) {
  done.value = job.state === 'done'
  if (done.value) emit('reload')
}
</script>

<template>
  <section class="wzjob">
    <p class="mt">Вкладку можно закрыть: установка идёт на сервере, ссылка вернёт сюда.</p>
    <!-- Другая вкладка повторила или пропустила экзит: свежее задание - заново, с опросом. -->
    <ExitJob v-if="state.job" :key="`${state.job.id}-${state.job.state}`" :transport="transport" :job="state.job" log-open @update="update" />
    <p v-else class="sub2" role="status">Установка запускается…</p>
  </section>
</template>
