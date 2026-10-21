<script setup lang="ts">
import {computed} from 'vue'
import {marked} from 'marked'
import DOMPurify from 'dompurify'
const props=defineProps<{text:string;sources?:{id:number}[]}>()
const emit=defineEmits<{cite:[id:number]}>()
const html=computed(()=>{
 const clean=DOMPurify.sanitize(marked.parse(props.text,{breaks:true}) as string,{FORBID_TAGS:['img','video','audio','iframe','form','input','button']})
 const root=document.createElement('div');root.innerHTML=clean
 const ids=new Set((props.sources||[]).map(s=>s.id))
 if(ids.size){
  const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT),nodes:Text[]=[]
  while(walker.nextNode())if(!(walker.currentNode.parentElement?.closest('a,code,pre,button')))nodes.push(walker.currentNode as Text)
  for(const node of nodes){
   const value=node.textContent||'',pattern=/\[(?:证据)?(\d+)\]/g,fragment=document.createDocumentFragment();let start=0,changed=false
   for(const match of value.matchAll(pattern)){
    if(!ids.has(Number(match[1])))continue
    fragment.append(document.createTextNode(value.slice(start,match.index)))
    const button=document.createElement('button');button.type='button';button.className='citation';button.dataset.cite=match[1];button.textContent=match[1];button.setAttribute('aria-label','查看证据'+match[1]);fragment.append(button)
    start=match.index!+match[0].length;changed=true
   }
   if(changed){fragment.append(document.createTextNode(value.slice(start)));node.replaceWith(fragment)}
  }
 }
 for(const link of Array.from(root.querySelectorAll('a'))){link.target='_blank';link.rel='noopener noreferrer'}
 return root.innerHTML
})
function clicked(event:MouseEvent){
 const target=(event.target as Element).closest<HTMLElement>('[data-cite]')
 if(target)emit('cite',Number(target.dataset.cite))
}
</script>
<template><div class="prose" v-html="html" @click="clicked"></div></template>
