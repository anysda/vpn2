<script setup lang="ts">
// Экран 4: где хранить бэкапы и закрытый ключ к ним. Ключ сервер выдаёт один раз, при первом
// показе экрана; он живёт только в памяти страницы, «Далее» - когда человек отметил, что сохранил.
import { computed, onMounted, ref } from 'vue'
import { call, refusal, type State } from '../api'

const props = defineProps<{ state: State }>()
const backup = props.state.draft.backup

const s3 = ref(backup.target === 's3')
const endpoint = ref(backup.s3.endpoint)
const bucket = ref(backup.s3.bucket)
const region = ref(backup.s3.region)
const access = ref('')
const secret = ref('')
const { s3_access_key: accessSet, s3_secret_key: secretSet } = props.state.secrets
const seen = props.state.backup_key === 'seen'
const key = ref(shownKey.private_key)
const saved = ref(false)
const error = ref('')
const copied = ref(false)

onMounted(async () => {
  if (seen || key.value) return
  try {
    shownKey = await call<typeof shownKey>('POST', '/api/backup-key')
    key.value = shownKey.private_key
  }
  catch (e) {
    error.value = refusal(e)
  }
})

async function copy() {
  copied.value = await navigator.clipboard?.writeText(key.value).then(() => true, () => false) ?? false
}

const s3Ready = computed(() => !!endpoint.value.trim() && !!bucket.value.trim() && (accessSet || !!access.value.trim()) && (secretSet || !!secret.value.trim()))
const ready = computed(() => (seen || (!!key.value && saved.value)) && (!s3.value || s3Ready.value))
defineExpose({
  ready,
  collect: () => ({
    patch: { backup: { target: s3.value ? 's3' as const : 'local' as const, s3: { endpoint: endpoint.value.trim(), bucket: bucket.value.trim(), region: region.value.trim() } } },
    secrets: s3.value ? { ...(access.value.trim() && { s3_access_key: access.value.trim() }), ...(secret.value.trim() && { s3_secret_key: secret.value.trim() }) } : {},
    // Открытая часть - какой именно ключ человек сохранил: сменённый другой вкладкой сервер отвергнет.
    then: seen ? undefined : () => call('POST', '/api/backup-key/seen', { recipient: shownKey.recipient }),
  }),
})
</script>

<script lang="ts">
// Ключ, уже показанный в этой вкладке: «Назад» и снова сюда не просят у сервера новый.
let shownKey = { private_key: '', recipient: '' }
</script>

<template>
  <section>
    <div class="seg sm" role="group" aria-label="Где хранить бэкапы">
      <button type="button" :class="{ on: !s3 }" @click="s3 = false">На этом сервере</button>
      <button type="button" :class="{ on: s3 }" @click="s3 = true">S3</button>
    </div>
    <p v-if="!s3" class="mt">Каждую ночь архив в /var/backups/anysda-vpn2, десять последних. S3 можно включить позже в config.yaml.</p>
    <template v-else>
      <label class="fl2"><span>Endpoint</span><input v-model="endpoint" class="in mono" placeholder="https://s3.eu-central-1.amazonaws.com" spellcheck="false"></label>
      <div class="wzpair">
        <label class="fl2"><span>Bucket</span><input v-model="bucket" class="in mono" placeholder="my-anysda-backups" spellcheck="false"></label>
        <label class="fl2"><span>Регион, если нужен</span><input v-model="region" class="in mono" placeholder="eu-central-1" spellcheck="false"></label>
      </div>
      <div class="wzpair">
        <label class="fl2"><span>Access key</span><input v-model="access" class="in mono" type="password" autocomplete="off" :placeholder="accessSet ? 'задан' : ''"></label>
        <label class="fl2"><span>Secret key</span><input v-model="secret" class="in mono" type="password" autocomplete="off" :placeholder="secretSet ? 'задан' : ''"></label>
      </div>
    </template>

    <div class="wzsec">
      <h3>Ключ бэкапов</h3>
      <p v-if="seen" class="mt">Ключ уже показан и сохранён вами. Ещё раз его не показать: на сервере его нет.</p>
      <template v-else>
        <p class="mt">
          Сохраните этот ключ вне сервера, например в менеджере паролей. <b>Без него бэкапы не расшифровать</b>, на сервере его нет,
          и больше он не покажется. <code>./deploy.sh restore</code> спросит его.
        </p>
        <div v-if="key" class="xkey wzbk">
          <code class="mono" data-testid="backup-key">{{ key }}</code>
          <button type="button" class="pill" @click="copy">{{ copied ? 'Скопировано' : 'Копировать' }}</button>
        </div>
        <p v-else-if="!error" class="sub2">ключ готовится…</p>
        <p v-if="error" role="alert" class="wzbad">{{ error }}</p>
        <label class="wzcheck"><input v-model="saved" type="checkbox" :disabled="!key"> Я сохранил ключ</label>
      </template>
    </div>
  </section>
</template>
