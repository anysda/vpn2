<script setup lang="ts">
import type { ClientDetail, Device } from '~/composables/useClients'
import { useClientDevices } from '~/composables/useClients'
import type { ConfigKind } from '~/composables/useDeviceAccess'
import { sizeBinRu } from '~/utils/format'
import type { Confirm } from './Ask.vue'

const props = defineProps<{ detail: ClientDetail, confirm: Confirm, reload: () => Promise<void> }>()
defineEmits<{ add: [], config: [dev: Device, kind: ConfigKind] }>()

const { removeDevice, reissueDevice } = useClientDevices()
const toast = useToast()
const mobile = useMobile()

const limitReached = computed(() => {
  const d = props.detail
  return d.deviceLimit !== null && d.devices.length >= d.deviceLimit
})

function askReissueDevice(dev: Device) {
  const d = props.detail
  props.confirm({
    title: 'Перевыпустить ключи девайса?',
    pre: 'Перевыпустить ключи девайса ',
    name: dev.name,
    post: '? После применения старые конфиги этого девайса перестанут работать.',
    act: 'Перевыпустить',
    fail: 'Ошибка перевыпуска',
    run: async () => {
      await reissueDevice(d.id, dev.id)
      toast.add({ title: 'Перевыпуск выполняется', color: 'info' })
      await props.reload()
    },
  })
}

function askRemoveDevice(dev: Device) {
  const d = props.detail
  props.confirm({
    title: 'Удалить девайс?',
    pre: 'Удалить девайс ',
    name: dev.name,
    post: '? Действие необратимо; конфиги перестанут работать после подтверждения отключения.',
    act: 'Удалить',
    danger: true,
    fail: 'Ошибка удаления',
    run: async () => {
      await removeDevice(d.id, dev.id)
      toast.add({ title: 'Отключение выполняется', color: 'info' })
      await props.reload()
    },
  })
}
</script>

<template>
  <div class="sub">
    <h3>Девайсы {{ detail.devices.length }} / {{ detail.deviceLimit ?? '∞' }}</h3>
    <span :title="limitReached ? 'Достигнут лимит девайсов' : undefined">
      <button
        type="button"
        class="pill"
        :disabled="limitReached"
        @click="$emit('add')"
      >+ девайс</button>
    </span>
  </div>
  <div class="devl scroll">
    <div v-for="dev in detail.devices" :key="dev.id" class="dev" data-testid="device-row">
      <!-- телефон: имя и ↻ 🗑 строкой, под ними три протокола -->
      <template v-if="mobile">
        <div class="kb dvt">
          <PanelDeviceName :name="dev.name">
            <small :class="{ tr: dev.rxTotal + dev.txTotal > 0 }">{{ dev.rxTotal + dev.txTotal > 0 ? sizeBinRu(dev.rxTotal + dev.txTotal) : 'нет трафика' }}</small>
          </PanelDeviceName>
          <button
            type="button"
            class="ic"
            aria-label="Перевыпустить ключи девайса"
            @click="askReissueDevice(dev)"
          >
            <svg class="i" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5" /></svg>
          </button>
          <button
            type="button"
            class="ic"
            aria-label="Удалить девайс"
            @click="askRemoveDevice(dev)"
          >
            <svg class="i" viewBox="0 0 24 24"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13" /></svg>
          </button>
        </div>
        <div class="kb dk3">
          <button type="button" @click="$emit('config', dev, 'wg')">
            WG
          </button>
          <button type="button" @click="$emit('config', dev, 'ovpn')">
            OVPN
          </button>
          <button type="button" @click="$emit('config', dev, 'ike')">
            IKEv2
          </button>
        </div>
      </template>
      <template v-else>
        <PanelDeviceName :name="dev.name">
          <small
            :class="{ tr: dev.rxTotal + dev.txTotal > 0 }"
            :title="dev.rxTotal + dev.txTotal > 0 ? `↓ ${sizeBinRu(dev.rxTotal)} ↑ ${sizeBinRu(dev.txTotal)}` : undefined"
          >{{ dev.rxTotal + dev.txTotal > 0 ? sizeBinRu(dev.rxTotal + dev.txTotal) : 'нет трафика' }}</small>
        </PanelDeviceName>
        <div class="kb">
          <button type="button" @click="$emit('config', dev, 'wg')">
            WG
          </button>
          <button type="button" @click="$emit('config', dev, 'ovpn')">
            OVPN
          </button>
          <button type="button" @click="$emit('config', dev, 'ike')">
            IKEv2
          </button>
          <button
            type="button"
            class="ic"
            title="Перевыпустить ключи девайса"
            aria-label="Перевыпустить ключи девайса"
            @click="askReissueDevice(dev)"
          >
            <svg class="i" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5" /></svg>
          </button>
          <button
            type="button"
            class="ic"
            title="Удалить девайс"
            aria-label="Удалить девайс"
            @click="askRemoveDevice(dev)"
          >
            <svg class="i" viewBox="0 0 24 24"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13" /></svg>
          </button>
        </div>
      </template>
    </div>
    <div v-if="!detail.devices.length" class="dev nod">
      Пока нет девайсов — добавь первый.
    </div>
  </div>
</template>
