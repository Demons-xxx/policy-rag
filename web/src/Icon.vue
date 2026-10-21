<script setup lang="ts">
defineProps<{name:string}>()
const paths:Record<string,string>={
 chat:'M4 4h16v12H9l-5 4V4Z',plus:'M12 5v14M5 12h14',upload:'M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6',
 book:'M4 4h7a3 3 0 0 1 3 3v14a3 3 0 0 0-3-3H4V4Zm10 3a3 3 0 0 1 3-3h3v14h-3a3 3 0 0 0-3 3',
 file:'M6 3h8l4 4v14H6V3Zm8 0v5h4M9 12h6M9 16h6',send:'m5 12 7-7 7 7M12 5v15',close:'m6 6 12 12M6 18 18 6',
 search:'M10 17a7 7 0 1 0 0-14 7 7 0 0 0 0 14Zm5-2 6 6',chevron:'m9 5 7 7-7 7',check:'m5 12 4 4L19 6',
 panel:'M3 4h18v16H3V4Zm12 0v16',copy:'M9 8h11v13H9V8ZM5 16H3V3h11v2',flow:'M4 4h5v5H4V4Zm11 11h5v5h-5v-5ZM9 6h8v9M6 9v8h9',
 arrow:'M4 12h16m-6-6 6 6-6 6',menu:'M4 6h16M4 12h16M4 18h16',stop:'M6 6h12v12H6V6Z',refresh:'M20 7V3l-3 3a8 8 0 1 0 2 11M20 3h-5',
 trash:'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7'}
</script>
<template><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="paths[name]||paths.file"/></svg></template>
