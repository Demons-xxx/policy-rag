<script setup lang="ts">
import {computed,ref,watch} from 'vue'
const props=defineProps<{url:string;pages:number}>()
const emit=defineEmits<{page:[value:number]}>()
const page=ref(1),draft=ref('1'),zoom=ref(false),failed=ref(false),loading=ref(true),attempt=ref(0)
const count=computed(()=>Math.max(1,Math.trunc(Number(props.pages)||1)))
const base=computed(()=>props.url.split('#')[0].replace(/\/pdf$/,''))
const image=computed(()=>`${base.value}/pages/${page.value}/image${attempt.value?'?retry='+attempt.value:''}`)
function setPage(value:number){page.value=Math.max(1,Math.min(count.value,Math.trunc(value)||1));draft.value=String(page.value)}
function commit(){setPage(Number(draft.value))}
function retry(){attempt.value++;failed.value=false;loading.value=true}
function loaded(event:Event,error=false){if((event.target as HTMLImageElement).getAttribute('src')!==image.value)return;failed.value=error;loading.value=false}
watch(()=>props.url,()=>{setPage(Number(props.url.match(/#page=(\d+)/)?.[1]||1));zoom.value=false;attempt.value=0},{immediate:true})
watch(count,()=>setPage(page.value))
watch(page,value=>emit('page',value),{immediate:true})
watch(image,()=>{failed.value=false;loading.value=true})
</script>

<template>
 <div class="pdf-preview">
  <div class="pdf-controls"><button :disabled="page<=1" aria-label="上一页" @click="setPage(page-1)">‹</button><span>第 <input v-model="draft" aria-label="PDF页码" type="number" min="1" :max="count" step="1" @change="commit" @keydown.enter.prevent="commit"> / {{count}} 页</span><button :disabled="page>=count" aria-label="下一页" @click="setPage(page+1)">›</button><button :aria-pressed="zoom" @click="zoom=!zoom">{{zoom?'适合宽度':'放大'}}</button></div>
  <div class="pdf-paper" :aria-busy="loading"><p v-if="loading" class="pdf-status" role="status">正在加载第{{page}}页</p><div v-if="failed" class="pdf-status" role="status">原文页未能加载。<button class="quiet-button" @click="retry">重试加载</button></div><img v-show="!failed&&!loading" :key="image" :src="image" :alt="`PDF原文第${page}页`" :style="{width:zoom?'180%':'100%'}" @load="loaded($event)" @error="loaded($event,true)"></div>
 </div>
</template>

<style scoped>
.pdf-preview{display:flex;flex-direction:column;flex:1;min-height:0;min-width:0;background:#edf0eb}
.pdf-controls{display:flex;align-items:center;justify-content:center;gap:8px;padding:8px 6px;flex-shrink:0;background:#f7f8f5;border-bottom:1px solid var(--line);font-size:13px}
.pdf-controls button{padding:3px 7px;border-radius:5px}.pdf-controls button:hover{background:#e3ebe4}.pdf-controls input{width:55px;border:1px solid var(--line);border-radius:4px;padding:3px;text-align:center;background:white}
.pdf-paper{flex:1;min-height:0;overflow:auto;padding:10px}.pdf-paper img{display:block;height:auto;background:white;box-shadow:0 1px 5px #23352a15}.pdf-status{padding:15px;font-size:14px}
</style>
