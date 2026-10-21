import {computed,onBeforeUnmount,ref} from 'vue'

export function usePaneWidths(hasPanel:()=>boolean){
 const viewport=ref(window.innerWidth),sidebar=ref(236),detail=ref(420),dragging=ref(false)
 try{const saved=JSON.parse(localStorage.getItem('policy-rag-pane-widths')||'{}');if(Number.isFinite(saved.sidebar))sidebar.value=Math.max(180,Math.min(360,saved.sidebar));if(Number.isFinite(saved.detail))detail.value=Math.max(320,Math.min(760,saved.detail))}catch{}
 const sidebarMax=computed(()=>Math.min(360,Math.max(180,viewport.value-400-(hasPanel()&&viewport.value>1150?320:0))))
 const sidebarWidth=computed(()=>Math.min(sidebar.value,sidebarMax.value))
 const detailMax=computed(()=>Math.min(760,Math.max(320,viewport.value-sidebarWidth.value-400)))
 const detailWidth=computed(()=>Math.min(detail.value,detailMax.value))
 let cleanup:(()=>void)|null=null,frame=0
 function save(){try{localStorage.setItem('policy-rag-pane-widths',JSON.stringify({sidebar:sidebar.value,detail:detail.value}))}catch{}}
 function set(side:'sidebar'|'detail',width:number){const value=side==='sidebar'?sidebar:detail;value.value=Math.max(side==='sidebar'?180:320,Math.min(side==='sidebar'?sidebarMax.value:detailMax.value,width))}
 function start(event:PointerEvent,side:'sidebar'|'detail'){
  if(event.button!==0)return;cleanup?.();event.preventDefault()
  const handle=event.currentTarget as HTMLElement,startX=event.clientX,startWidth=side==='sidebar'?sidebarWidth.value:detailWidth.value
  let x=startX;dragging.value=true;handle.setPointerCapture(event.pointerId)
  const update=()=>{frame=0;set(side,startWidth+(x-startX)*(side==='sidebar'?1:-1))}
  const move=(e:PointerEvent)=>{x=e.clientX;if(!frame)frame=requestAnimationFrame(update)}
  const end=()=>{if(frame)cancelAnimationFrame(frame);update();dragging.value=false;handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',end);handle.removeEventListener('pointercancel',end);handle.removeEventListener('lostpointercapture',end);if(handle.hasPointerCapture(event.pointerId))handle.releasePointerCapture(event.pointerId);cleanup=null;save()}
  handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',end);handle.addEventListener('pointercancel',end);handle.addEventListener('lostpointercapture',end);cleanup=end
 }
 function key(event:KeyboardEvent,side:'sidebar'|'detail'){
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault()
  const current=side==='sidebar'?sidebarWidth.value:detailWidth.value
  set(side,event.key==='Home'?(side==='sidebar'?180:320):event.key==='End'?(side==='sidebar'?sidebarMax.value:detailMax.value):current+(event.key==='ArrowRight'?1:-1)*(side==='sidebar'?1:-1)*(event.shiftKey?40:10));save()
 }
 function reset(side:'sidebar'|'detail'){set(side,side==='sidebar'?236:420);save()}
 const resize=()=>{cleanup?.();viewport.value=window.innerWidth}
 window.addEventListener('resize',resize)
 onBeforeUnmount(()=>{cleanup?.();window.removeEventListener('resize',resize)})
 return {sidebarWidth,detailWidth,sidebarMax,detailMax,dragging,start,key,reset}
}
