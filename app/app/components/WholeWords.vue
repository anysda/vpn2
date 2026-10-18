<script setup lang="ts">
// Текст, где слова идут вперемешку с доменами, адресами и ключами. Слово переносится только
// целиком и посреди не рвётся никогда; в любом месте рвётся лишь не слово - кусок с цифрой,
// точкой, двоеточием, @, / или _ внутри либо буквы подряд длиннее любого слова, - иначе такой
// кусок вылез бы за свой блок.
const props = defineProps<{ text: string }>()
const NOT_WORD = /[\d.:@/\\_=+#]|\p{L}{25}/u
const parts = computed(() => props.text.split(/(\s+)/).map(t => ({ t, tk: NOT_WORD.test(t.replace(/[.,:;!?»)]+$/, '')) })))
</script>

<template>
  <template v-for="(p, i) in parts" :key="i"><span v-if="p.tk" class="tk">{{ p.t }}</span><template v-else>{{ p.t }}</template></template>
</template>

<style>
.tk{overflow-wrap:anywhere}
</style>
