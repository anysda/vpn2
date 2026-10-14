<script setup lang="ts">
// Блокировка сервисов AdGuard для всех клиентов. Каталог — весь, что знает
// AdGuard; список заблокированных уходит целиком, нажатия — по очереди.
interface View { all: { id: string, name: string }[], blocked: string[] }

const toast = useToast()
const { data, error, refresh } = useFetch<View>('/api/dns/services', { server: false, lazy: true })
const blocked = ref(new Set<string>())
watch(data, (v) => {
  if (v) blocked.value = new Set(v.blocked)
}, { immediate: true })

const errText = (e: unknown) => {
  const x = e as { data?: { message?: string }, statusMessage?: string, message?: string }
  return x.data?.message ?? x.statusMessage ?? x.message ?? 'Ошибка'
}

const count = computed(() => data.value ? `Заблокировано ${blocked.value.size} из ${data.value.all.length}` : '')

let queue = Promise.resolve()
function save(ids: string[], done?: string) {
  queue = queue.then(async () => {
    try {
      await $fetch('/api/dns/services', { method: 'PUT', body: { ids } })
      if (done) toast.add({ title: done })
    }
    catch (e) {
      toast.add({ title: errText(e), color: 'error' })
      await refresh()
    }
  })
}

function toggle(id: string) {
  const s = new Set(blocked.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  blocked.value = s
  save([...s])
}

function allOff() {
  blocked.value = new Set()
  save([], 'Все сервисы разблокированы')
}
</script>

<template>
  <div>
    <div v-if="error && !data" class="card">
      <h3>{{ errText(error) }}</h3>
    </div>
    <div class="fhead svh">
      <div>
        <h3>Блокировка сервисов</h3>
        <p class="sub2">
          Для всех клиентов. Для одного клиента — во вкладке «Клиенты»
        </p>
      </div>
      <div class="ha">
        <span class="tag" style="height:36px;padding:0 14px">{{ count }}</span>
        <button type="button" class="pill" :disabled="!data" @click="allOff">
          Разблокировать все
        </button>
      </div>
    </div>
    <div class="svg2">
      <div
        v-for="s in data?.all ?? []"
        :key="s.id"
        class="card sv"
        :class="{ on: blocked.has(s.id) }"
        role="switch"
        tabindex="0"
        :aria-checked="blocked.has(s.id)"
        :aria-label="s.name"
        @click="toggle(s.id)"
        @keydown.enter.prevent="toggle(s.id)"
        @keydown.space.prevent="toggle(s.id)"
      >
        <div class="st">
          <span class="lo">{{ s.name[0] }}</span><span class="tog" :class="{ on: blocked.has(s.id) }"><i /></span>
        </div>
        <b>{{ s.name }}</b><small>{{ blocked.has(s.id) ? 'заблокирован' : 'разрешён' }}</small>
      </div>
    </div>
  </div>
</template>
