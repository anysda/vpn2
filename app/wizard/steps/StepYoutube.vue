<script setup lang="ts">
// Экран 5: YouTube через российский адрес entry (youtube.route: zapret) или через экзиты.
import { ref } from 'vue'
import type { State } from '../api'

const props = defineProps<{ state: State }>()
const on = ref(props.state.draft.youtube)

defineExpose({ ready: true, collect: () => ({ patch: { youtube: on.value } }) })
</script>

<template>
  <section>
    <p class="mt">
      На российских IP YouTube не показывает рекламу. Включено - YouTube идёт прямо с этого сервера, блокировку DPI обходит zapret,
      и самый тяжёлый трафик не ест канал экзита. Выключено - YouTube идёт через экзиты, как всё остальное.
    </p>
    <div class="fld wzsw">
      <span>YouTube через РФ</span>
      <code>{{ on ? 'включено' : 'выключено' }}</code>
      <button type="button" class="tog" :class="{ on }" role="switch" :aria-checked="on" aria-label="YouTube через РФ" @click="on = !on"><i /></button>
    </div>
  </section>
</template>
