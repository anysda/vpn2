<script setup lang="ts">
defineProps<{ configured: boolean, sending: boolean, bot?: string, result?: string, buttonLabel: string }>()
defineEmits<{ send: [] }>()
const message = defineModel<string>({ required: true })
</script>

<template>
  <div class="card sc bc ov-broadcast">
    <div class="sh"><h3>Рассылка от бота</h3><span v-if="bot" class="tag">@{{ bot }}</span></div>
    <textarea v-model="message" maxlength="4096" placeholder="Текст сообщения всем клиентам с ботом" aria-label="Сообщение юзерам" />
    <div class="arow">
      <span v-if="!configured" class="hint">Сначала настройте Telegram-бота</span>
      <button
        type="button" class="pill w" style="margin-left:auto"
        :disabled="!configured || sending || !message.trim()" @click="$emit('send')"
      >{{ buttonLabel }}</button>
    </div>
    <p v-if="result" class="hint" role="status">{{ result }}</p>
  </div>
</template>

<style scoped>
/* Свои стили: карточка стоит на странице клиентов, обёртки .ov там нет. */
.bc{padding:20px 22px;display:flex;flex-direction:column;min-height:270px}
.sh{display:flex;justify-content:space-between;align-items:center;gap:12px}
.sh h3{color:var(--t1);font-size:15px}
.tag{display:inline-flex;align-items:center;height:24px;padding:0 10px;border-radius:999px;background:rgba(var(--fg),.06);color:var(--t2);font-size:11.5px;white-space:nowrap}
textarea{margin:14px 0 0;flex:1;min-height:120px;resize:none;border-radius:14px;padding:14px 16px;background:var(--sunk);border:1px solid rgba(var(--fg),.05);box-shadow:inset 0 2px 10px rgba(0,0,0,calc(.5*var(--shk)));color:var(--t1);font:400 14px/1.5 var(--font-sans);outline:0}
textarea:focus{border-color:rgba(var(--fg),.25)}
.arow{display:flex;gap:8px;margin-top:12px;align-items:center}
.arow .pill{height:40px}
.hint{font-size:12px;color:var(--t3)}
@media (max-width:720px){
  .bc{padding:20px 18px;min-height:0}
  textarea{flex:none}
}
</style>
