<script setup lang="ts">
// Список заблокированных уходит целиком, нажатия - по очереди.
interface View { all: { id: string, name: string }[], blocked: string[] }

const { data, load, act } = useDns<View>('/api/dns/services')
const blocked = ref(new Set<string>())
watch(data, v => v && (blocked.value = new Set(v.blocked)), { immediate: true })

let queue: Promise<unknown> = Promise.resolve()
function save(ids: Set<string>, done?: string) {
  blocked.value = ids
  queue = queue.then(async () => {
    if (!await act('save', () => $fetch('/api/dns/services', { method: 'PUT', body: { ids: [...ids] } }), done)) await load.refresh()
  })
}
function toggle(id: string) {
  const s = new Set(blocked.value)
  if (!s.delete(id)) s.add(id)
  save(s)
}
</script>

<template>
  <div>
    <DnsLoadError :of="load" />
    <DnsHead title="Блокировка сервисов" sub="Для всех клиентов. Для одного клиента — во вкладке «Клиенты»">
      <div class="ha">
        <span v-if="data" class="tag">Заблокировано {{ blocked.size }} из {{ data.all.length }}</span>
        <button type="button" class="pill" :disabled="!data" @click="save(new Set(), 'Все сервисы разблокированы')">
          Разблокировать все
        </button>
      </div>
    </DnsHead>
    <div class="mt-3 grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-3">
      <button
        v-for="s in data?.all ?? []" :key="s.id" type="button" role="switch" class="card text-left" :class="{ lit: blocked.has(s.id) }"
        :aria-checked="blocked.has(s.id)" :aria-label="s.name" @click="toggle(s.id)">
        <b class="block truncate font-medium">{{ s.name }}</b>
        <small class="text-xs text-(--t3)">{{ blocked.has(s.id) ? 'заблокирован' : 'разрешён' }}</small>
      </button>
    </div>
  </div>
</template>
