<script setup lang="ts">
// Сообщения фреймворка («Page not found: …») наружу не выводим: текст как в макете.
defineProps<{ error: { statusCode?: number } }>()

function handleError() {
  clearError({ redirect: '/' })
}

// Карточка поднимается, как карточки разделов.
const card = ref<HTMLElement>()
onMounted(() => {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return
  card.value?.animate([{ opacity: 0, transform: 'translateY(22px)' }, { opacity: 1, transform: 'none' }],
    { duration: 520, delay: 60, easing: 'cubic-bezier(.23,1,.32,1)', fill: 'backwards' })
})
</script>

<template>
  <div>
    <div class="light" />
    <div class="wrap">
      <main class="tab on bare">
        <div ref="card" class="card lgn errp">
          <svg class="i tri" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4l9 16H3zM12 10v4.5M12 17.2v.1" /></svg>
          <h2>Ошибка</h2>
          <b v-if="error.statusCode" class="big">{{ error.statusCode }}</b>
          <p>Что-то пошло не так.</p>
          <div class="mf" style="justify-content:center">
            <button type="button" class="pill" @click="handleError">
              На главную
            </button>
            <button type="button" class="pill w" @click="$router.go(0)">
              Обновить
            </button>
          </div>
        </div>
      </main>
    </div>
  </div>
</template>
