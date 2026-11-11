<script setup lang="ts">
import type { ClientDetail, Device } from '~/composables/useClients'
import type { ConfigKind } from '~/composables/useDeviceAccess'
import type { Ask } from './Ask.vue'

/** Девайсы клиента: конфиги, перевыпуск, удаление. `ask` - вопрос карточки перед действием. */
const props = defineProps<{
  detail: ClientDetail
  ask: (a: Omit<Ask, 'run'>, run: () => Promise<unknown>, started: string) => void
}>()
defineEmits<{ added: [] }>()

const KINDS = [['wg', 'WG'], ['ovpn', 'OVPN'], ['ike', 'IKEv2']] as const
const newDevOpen = ref(false)
const cfgOpen = ref(false)
const cfg = ref<{ device: Device, kind: ConfigKind }>()

function openConfig(device: Device, kind: ConfigKind) {
  cfg.value = { device, kind }
  cfgOpen.value = true
}

const limitReached = computed(() => {
  const d = props.detail
  return d.deviceLimit !== null && d.devices.length >= d.deviceLimit
})
const used = (dev: Device) => dev.rxTotal + dev.txTotal

function askReissueDevice(dev: Device) {
  props.ask({
    title: 'Перевыпустить ключи девайса?',
    pre: 'Перевыпустить ключи девайса ',
    name: dev.name,
    post: '? После применения старые конфиги этого девайса перестанут работать.',
    act: 'Перевыпустить',
    fail: 'Ошибка перевыпуска',
  }, () => reissueDevice(props.detail.id, dev.id), 'Перевыпуск выполняется')
}

function askRemoveDevice(dev: Device) {
  props.ask({
    title: 'Удалить девайс?',
    pre: 'Удалить девайс ',
    name: dev.name,
    post: '? Действие необратимо; конфиги перестанут работать после подтверждения отключения.',
    act: 'Удалить',
    danger: true,
    fail: 'Ошибка удаления',
  }, () => removeDevice(props.detail.id, dev.id), 'Отключение выполняется')
}
</script>

<template>
  <div class="sub">
    <h3>Девайсы {{ detail.devices.length }} / {{ detail.deviceLimit ?? '∞' }}</h3>
    <span :title="limitReached ? 'Достигнут лимит девайсов' : undefined">
      <button type="button" class="pill" :disabled="limitReached" @click="newDevOpen = true">+ девайс</button>
    </span>
  </div>
  <div class="devl scroll">
    <!-- телефон: имя и ↻ 🗑 строкой, под ними три протокола (порядок — стилями) -->
    <div v-for="dev in detail.devices" :key="dev.id" class="dev" data-testid="device-row">
      <div>
        <b>{{ dev.name }}</b>
        <small
          :class="{ tr: used(dev) }"
          :title="used(dev) ? `↓ ${sizeBinRu(dev.rxTotal)} ↑ ${sizeBinRu(dev.txTotal)}` : undefined">
          {{ used(dev) ? sizeBinRu(used(dev)) : 'нет трафика' }}
        </small>
      </div>
      <div class="kb">
        <button v-for="[kind, label] in KINDS" :key="kind" type="button" @click="openConfig(dev, kind)">
          {{ label }}
        </button>
        <button
          type="button"
          class="ic"
          title="Перевыпустить ключи девайса"
          aria-label="Перевыпустить ключи девайса"
          @click="askReissueDevice(dev)">
          <svg class="i" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5" /></svg>
        </button>
        <button
          type="button"
          class="ic"
          title="Удалить девайс"
          aria-label="Удалить девайс"
          @click="askRemoveDevice(dev)">
          <svg class="i" viewBox="0 0 24 24"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13" /></svg>
        </button>
      </div>
    </div>
    <div v-if="!detail.devices.length" class="dev nod">
      Пока нет девайсов — добавь первый.
    </div>
  </div>

  <ClientsNewDevice v-model:open="newDevOpen" :client-id="detail.id" @added="$emit('added')" />
  <ClientsDeviceConfig v-if="cfg" v-model:open="cfgOpen" :client-id="detail.id" :device="cfg.device" :kind="cfg.kind" />
</template>
