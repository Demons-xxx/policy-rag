<script setup lang="ts">
import {ref,computed,reactive,onMounted,onBeforeUnmount,nextTick,watch} from 'vue'
import MarkdownText from './MarkdownText.vue'
import {selectFiles} from './fileSelection'
import {usePaneWidths} from './paneWidths'
import Icon from './Icon.vue'
import PdfPreview from './PdfPreview.vue'

type Source={id:number;doc_id:string;file:string;pages:number[];focus_page?:number|null;title?:string;text:string;article?:string|null;chapter?:string|null;doc_no?:string|null;version_label?:string|null}
type Turn={id:string;question:string;answer:string;sources:Source[];events:any[];error?:string;seconds?:number;busy?:boolean;status?:string}
type Session={id:string;title:string;turns:Turn[]}
type PolicyDocument={id:string;name?:string;title?:string;doc_no?:string|null;version_label?:string|null;department?:string|null;status?:string;pages?:number;has_pdf?:boolean|number;effective_from?:string|null;effective_to?:string|null}
type Task={id:string;files?:string[];name?:string;status?:string;phase?:string;error?:string;active?:boolean;units?:number}

const view=ref<'chat'|'library'>('chat'),navOpen=ref(false),librarySearch=ref('')
const info=ref<any>(null),documents=ref<PolicyDocument[]>([]),tasks=ref<Task[]>([])
const sessions=ref<Session[]>([]),sessionId=ref(''),input=ref(''),busy=ref(false),flow=ref(false)
const panel=ref<'source'|null>(null),selectedSource=ref<Source|null>(null)
const sourceMode=ref<'text'|'pdf'>('text'),selectedPdf=ref(''),toast=ref(''),conversation=ref<HTMLElement|null>(null),composer=ref<HTMLTextAreaElement|null>(null)
const uploading=ref(false),uploadOpen=ref(false),files=ref<File[]>([]),uploadProgress=ref(0),fileInput=ref<HTMLInputElement|null>(null)
const form=reactive({title:'',doc_no:'',version_label:'',effective_from:'',effective_to:'',department:''})
const libraryLoading=ref(false),libraryError=ref(''),connectionLoading=ref(false),connectionError=ref(''),atBottom=ref(true),retryBusy=ref('')
const pdfPage=ref(1)
let controller:AbortController|null=null,toastTimer:ReturnType<typeof setTimeout>|null=null
let uploadRequest:XMLHttpRequest|null=null,disposed=false,storageWarning=false,libraryRequest:Promise<void>|null=null
const panes=usePaneWidths(()=>Boolean(panel.value))
const connectionLabel=computed(()=>connectionLoading.value?'正在连接':connectionError.value?'连接已断开':info.value?.model_ready?'服务已连接':'服务准备中')
const current=computed(()=>sessions.value.find(s=>s.id===sessionId.value))
const title=computed(()=>view.value==='library'?'资料库':current.value?.title||'新对话')
const filteredDocuments=computed(()=>{const text=librarySearch.value.trim().toLowerCase();return documents.value.filter(d=>`${d.title||''} ${d.name||''} ${d.doc_no||''} ${d.version_label||''} ${d.department||''}`.toLowerCase().includes(text))})
const examples=computed(()=>{const list=info.value?.examples;return Array.isArray(list)&&list.length?list.slice(0,3):['实验室危化品怎么领用？','科研经费出差住宿费能报多少？','教职工请假由谁审批？']})
const currentHasPdf=computed(()=>sourceHasPdf(selectedSource.value))
function newId(){return globalThis.crypto?.randomUUID?.()||Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)}
function notice(text:string){if(disposed)return;toast.value=text;if(toastTimer)clearTimeout(toastTimer);toastTimer=setTimeout(()=>toast.value='',4000)}
async function api(path:string,options?:RequestInit){const timeout=new AbortController(),timer=setTimeout(()=>timeout.abort(),15000);try{const r=await fetch(path,{...options,signal:timeout.signal});if(!r.ok){let text=`请求未完成（${r.status}）`;try{const detail=(await r.json()).detail;text=typeof detail==='string'?detail:text}catch{}throw new Error(text)}return await r.json()}catch(e:any){throw new Error(e.name==='AbortError'?'连接超时，请重试':e instanceof TypeError?'连接中断，请检查网络':e.message)}finally{clearTimeout(timer)}}
function persist(){try{const values=sessions.value.filter(s=>s.turns.length||s.id===sessionId.value).slice(0,15).map(s=>({...s,turns:s.turns.map(t=>({...t,busy:false,error:t.busy?'上次回答尚未完成':t.error}))}));localStorage.setItem('policy-rag-sessions',JSON.stringify(values));localStorage.setItem('policy-rag-session-id',sessionId.value)}catch{if(!storageWarning){storageWarning=true;notice('浏览器记录未能保存，请复制需要保留的回答')}}}
function newChat(){if(busy.value)return;view.value='chat';navOpen.value=false;sessionId.value=newId();sessions.value.unshift({id:sessionId.value,title:'新对话',turns:[]});panel.value=null;selectedSource.value=null;input.value='';atBottom.value=true;persist();nextTick(()=>composer.value?.focus())}
function selectSession(s:Session){if(busy.value)return;view.value='chat';sessionId.value=s.id;navOpen.value=false;atBottom.value=true;persist();jumpToLatest()}
function deleteSession(s:Session){
 if(busy.value||!sessions.value.some(item=>item.id===s.id))return
 if(!window.confirm(`删除“${s.title}”？\n将清除此浏览器保存的该条对话，无法恢复。`))return
 sessions.value=sessions.value.filter(item=>item.id!==s.id)
 if(sessionId.value===s.id){selectedSource.value=null;selectedPdf.value='';newChat()}
 else persist()
}
function answerCite(id:number,turn:Turn){const source=turn.sources.find(s=>s.id===id);if(source)openSource(source)}
function sourceHasPdf(source:Source|null|undefined){if(!source)return false;const doc=documentFor(source.doc_id);if(doc)return doc.has_pdf===true||doc.has_pdf===1;return /\.pdf$/i.test(source.file||'')}
function documentFor(docId?:string|null){return documents.value.find(d=>d.id===docId)}
function openSource(source:Source){
 selectedSource.value=source
 pdfPage.value=source.focus_page&&source.pages?.includes(source.focus_page)?source.focus_page:source.pages?.[0]||1
 selectedPdf.value=`/api/documents/${encodeURIComponent(source.doc_id)}/pdf#page=${pdfPage.value}`
 sourceMode.value='text';panel.value='source'
}
function openDocument(d:PolicyDocument){
 selectedSource.value={id:0,doc_id:d.id,file:d.name||d.title||'制度文档',pages:[1],title:d.title||d.name||'',text:''}
 selectedPdf.value=`/api/documents/${encodeURIComponent(d.id)}/pdf#page=1`
 sourceMode.value=Boolean(d.has_pdf)?'pdf':'text';panel.value='source'
}
function promptPreview(e:any){const messages=e.messages||[];return `系统要求：\n${messages[0]?.content?.slice(0,240)||''}\n\n问题：\n${messages.at(-1)?.content?.slice(0,1400)||''}`}
function promptFull(e:any){return (e.messages||[]).map((m:any)=>`${({system:'系统提示词',user:'用户提示词',assistant:'助手消息'} as Record<string,string>)[m.role]||m.role}：\n${m.content}`).join('\n\n')}
function stageLabel(e:any){return ({routing:'问题判断',query:'问题解析',vectors:'向量检索',keywords:'关键词检索',evidence:'证据筛选',prompt:'提示词',generating:'生成回答'} as Record<string,string>)[e.stage]||e.stage}
function queryText(e:any){const parts:string[]=[];if(e.rewritten)parts.push(e.rewritten);const filters=e.filters||{};if(filters.department)parts.push('部门：'+filters.department);if(filters.as_of)parts.push('时点：'+filters.as_of);return parts.join(' · ')||e.question||''}
function trackScroll(){if(conversation.value)atBottom.value=conversation.value.scrollHeight-conversation.value.scrollTop-conversation.value.clientHeight<100}
async function scroll(){if(!atBottom.value||view.value!=='chat')return;await nextTick();if(conversation.value)conversation.value.scrollTop=conversation.value.scrollHeight}
async function jumpToLatest(){atBottom.value=true;await nextTick();if(conversation.value)conversation.value.scrollTop=conversation.value.scrollHeight}
function statusLabel(status?:string){return ({active:'现行',superseded:'已被替代',repealed:'已废止'} as Record<string,string>)[status||'']||status||'未知'}
function taskStatus(task:Task){return ({queued:'排队中',running:'处理中',done:'已入库',failed:'失败'} as Record<string,string>)[task.status||'']||task.status||(task.active?'处理中':'已结束')}
function taskLabel(task:Task){return task.files?.join('、')||task.name||task.id}
function taskBadgeClass(task:Task){return {success:task.status==='done',processing:task.active||task.status==='running'||task.status==='queued'}}
function normalizeSource(s:any):Source{return {id:Number(s?.id)||0,doc_id:String(s?.doc_id||''),file:String(s?.file||s?.title||'制度文档'),pages:Array.isArray(s?.pages)&&s.pages.length?s.pages.filter((p:any)=>Number.isInteger(p)&&p>0):[1],focus_page:typeof s?.focus_page==='number'?s.focus_page:null,title:String(s?.title||s?.file||''),text:String(s?.text||''),article:s?.article||null,chapter:s?.chapter||null,doc_no:s?.doc_no||null,version_label:s?.version_label||null}}
async function ask(questionOverride?:string){
 if(busy.value)return
 const question=(questionOverride??input.value).trim()
 if(question.length<1||question.length>500){notice('请输入1—500字');return}
 if(connectionError.value||!info.value?.model_ready){notice('服务尚未连接，请先重试连接');return}
 if(!current.value)newChat()
 view.value='chat';navOpen.value=false;input.value='';busy.value=true
 const turn=reactive<Turn>({id:newId(),question,answer:'',sources:[],events:[],busy:true,status:'正在理解问题'})
 const session=current.value!;session.turns.push(turn);if(session.title==='新对话')session.title=question.slice(0,22)
 const historyTurns=Math.max(1,Math.floor((info.value?.history_messages??8)/2))
 const history=session.turns.filter(t=>t.id!==turn.id&&!t.busy&&t.answer&&!t.error).slice(-historyTurns).flatMap(t=>[{role:'user',content:t.question},{role:'assistant',content:t.answer.slice(0,4000)}])
 panel.value=null;atBottom.value=true;await nextTick();if(conversation.value)conversation.value.scrollTop=conversation.value.scrollHeight
 controller=new AbortController();let pending='',buffer='',flushTimer:ReturnType<typeof setTimeout>|null=null,completed=false
 function flush(){if(pending){turn.answer+=pending;pending='';if(sessionId.value===session.id)scroll()}flushTimer=null}
 function receive(e:any){if(e.stage==='answer'){pending+=e.text;if(!flushTimer)flushTimer=setTimeout(flush,80)}
  else if(e.stage==='waiting')turn.status=e.message
  else if(e.stage==='routing')turn.status='正在理解问题'
  else if(e.stage==='generating')turn.status='正在生成回答'
  else if(e.stage==='sources')turn.sources=(e.items||[]).map(normalizeSource)
  else if(e.stage==='done'){completed=true;turn.seconds=e.seconds;if(e.output_truncated)turn.error='本次回答达到长度上限';}
  else if(e.stage==='error')turn.error=e.message
  else turn.events.push(e)
 }
 try{
  const response=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question,history,run_tag:'web'}),signal:controller.signal})
  if(!response.ok){let message='问答请求未完成';try{message=(await response.json()).detail||message}catch{}throw new Error(message)}
  if(!response.body)throw new Error('没有收到回答数据，请重新提问')
  const reader=response.body.getReader(),decoder=new TextDecoder()
  while(true){const {done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let i:number;while((i=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,i);buffer=buffer.slice(i+1);if(line.trim())receive(JSON.parse(line))}}
  buffer+=decoder.decode();if(buffer.trim())receive(JSON.parse(buffer))
  if(!completed&&!turn.error)turn.error='连接中断，回答尚未完成，可重新提问'
 }catch(e:any){turn.error=e.name==='AbortError'?'已停止':e instanceof TypeError?'连接中断，请检查网络后重试':e.message}
 finally{if(flushTimer)clearTimeout(flushTimer);flush();busy.value=false;turn.busy=false;controller=null;persist()}
}
async function copy(turn:Turn){try{await navigator.clipboard.writeText(turn.answer);notice('已复制')}catch{notice('请选中回答后复制')}}
async function loadLibrary(){
 if(disposed)return;if(libraryRequest)return libraryRequest
 libraryRequest=(async()=>{libraryLoading.value=true;libraryError.value='';try{const [d,t]=await Promise.allSettled([api('/api/documents'),api('/api/ingest')]);if(disposed)return;if(d.status==='fulfilled')documents.value=d.value.documents||[];else libraryError.value=d.reason.message;if(t.status==='fulfilled')tasks.value=t.value.tasks||[];else libraryError.value=libraryError.value||t.reason.message}catch(e:any){libraryError.value=e.message}finally{libraryLoading.value=false;libraryRequest=null}})()
 return libraryRequest
}
async function loadInfo(){if(connectionLoading.value||disposed)return;connectionLoading.value=true;try{const latest=await api('/api/info');if(disposed)return;info.value=latest;connectionError.value=''}catch(e:any){connectionError.value=e.message}finally{connectionLoading.value=false}}
function refreshConnection(){Promise.all([loadInfo(),loadLibrary()])}
function visibilityChanged(){if(!document.hidden){loadInfo();loadLibrary()}}
function acceptFiles(list:FileList|File[]|null){if(!list||uploading.value)return;const selected=selectFiles(files.value,Array.from(list));if(selected.error)notice(selected.error);else files.value=selected.files;if(fileInput.value)fileInput.value.value=''}
function drop(event:DragEvent){event.preventDefault();acceptFiles(event.dataTransfer?.files||null)}
function uploadMetadata(){const meta:Record<string,string>={};const title=form.title.trim();if(!title)return null;meta.title=title;if(form.doc_no.trim())meta.doc_no=form.doc_no.trim();if(form.version_label.trim())meta.version_label=form.version_label.trim();if(form.effective_from)meta.effective_from=form.effective_from;if(form.effective_to)meta.effective_to=form.effective_to;if(form.department.trim())meta.department=form.department.trim();return meta}
async function upload(){
 if(uploading.value)return
 const meta=uploadMetadata()
 if(!files.value.length||!meta){notice('请选择文件并填写制度名称');return}
 const data=new FormData();files.value.forEach(f=>data.append('files',f));data.append('metadata',JSON.stringify(meta))
 uploading.value=true;uploadProgress.value=0
 try{await new Promise<void>((resolve,reject)=>{const xhr=new XMLHttpRequest();uploadRequest=xhr;xhr.open('POST','/api/ingest');xhr.timeout=600000;xhr.upload.onprogress=e=>{if(e.lengthComputable)uploadProgress.value=Math.round(e.loaded/e.total*100)};xhr.onload=()=>{let result:any={};try{result=JSON.parse(xhr.responseText)}catch{}xhr.status>=200&&xhr.status<300?resolve():reject(new Error(typeof result.detail==='string'?result.detail:'上传未完成'))};xhr.onerror=()=>reject(new Error('连接中断，文件已保留，可重新上传'));xhr.ontimeout=()=>reject(new Error('上传超时，文件已保留'));xhr.onabort=()=>reject(new Error('上传已取消'));xhr.send(data)})
  files.value=[];uploadOpen.value=false;Object.assign(form,{title:'',doc_no:'',version_label:'',effective_from:'',effective_to:'',department:''});notice('资料已加入处理队列');await loadLibrary()
 }catch(e:any){notice(e.message)}finally{uploading.value=false;uploadRequest=null}
}
async function retry(task:Task){if(retryBusy.value||task.active)return;retryBusy.value=task.id;try{await api(`/api/ingest/${task.id}/retry`,{method:'POST'});await loadLibrary();notice('任务已重新加入处理队列')}catch(e:any){notice(e.message)}finally{retryBusy.value=''}}
function keydown(event:KeyboardEvent){if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();ask()}}
watch(input,()=>nextTick(()=>{if(composer.value){composer.value.style.height='auto';composer.value.style.height=Math.min(composer.value.scrollHeight,180)+'px'}}))
function escape(event:KeyboardEvent){if(event.key!=='Escape'||event.isComposing)return;if(uploadOpen.value&&!uploading.value)uploadOpen.value=false;else if(panel.value)panel.value=null;else navOpen.value=false}
onMounted(async()=>{
 try{const saved=JSON.parse(localStorage.getItem('policy-rag-sessions')||'[]');if(Array.isArray(saved))sessions.value=saved.filter(s=>typeof s?.id==='string'&&typeof s?.title==='string'&&Array.isArray(s.turns)).slice(0,15).map(s=>({...s,turns:s.turns.filter((t:any)=>typeof t?.id==='string'&&typeof t.question==='string'&&typeof t.answer==='string').map((t:any)=>({...t,busy:false,error:t.busy?'上次回答尚未完成':t.error,sources:Array.isArray(t.sources)?t.sources.filter((v:any)=>typeof v?.doc_id==='string'&&typeof v.file==='string').map((v:any)=>({...v,pages:Array.isArray(v.pages)?v.pages.filter((p:any)=>Number.isInteger(p)&&p>0):[1]})):[],events:Array.isArray(t.events)?t.events.filter((e:any)=>typeof e?.stage==='string'&&(!e.messages||Array.isArray(e.messages))):[]}))}))
 const last=localStorage.getItem('policy-rag-session-id');const restored=sessions.value.find(s=>s.id===last)||sessions.value.find(s=>s.turns.length);if(restored){sessionId.value=restored.id;nextTick(jumpToLatest)}else newChat()
 }catch{newChat();notice('历史记录未能读取，已打开新对话')}
 document.addEventListener('visibilitychange',visibilityChanged);window.addEventListener('keydown',escape);await Promise.all([loadInfo(),loadLibrary()])
})
onBeforeUnmount(()=>{disposed=true;controller?.abort();uploadRequest?.abort();document.removeEventListener('visibilitychange',visibilityChanged);window.removeEventListener('keydown',escape);if(toastTimer)clearTimeout(toastTimer)})
</script>

<template>
 <div class="workspace" :class="{'has-panel':panel,'nav-open':navOpen,'resizing':panes.dragging.value}" :style="{'--sidebar-width':panes.sidebarWidth.value+'px','--detail-width':panes.detailWidth.value+'px'}">
  <button v-if="navOpen" class="nav-backdrop" aria-label="关闭导航" @click="navOpen=false"></button>
  <aside class="sidebar">
   <div class="pane-resizer sidebar-resizer" role="separator" tabindex="0" aria-label="调整导航宽度" aria-orientation="vertical" :aria-valuenow="panes.sidebarWidth.value" aria-valuemin="180" :aria-valuemax="panes.sidebarMax.value" title="拖动调整宽度，双击恢复" @pointerdown="panes.start($event,'sidebar')" @keydown="panes.key($event,'sidebar')" @dblclick="panes.reset('sidebar')"></div>
   <div class="brand" aria-label="PolicyRAG 政策制度问答"><span class="brand-mark"><span></span><span></span><span></span><span></span></span><div class="brand-text"><strong>PolicyRAG</strong><span>政策制度问答</span></div></div>
   <nav><button class="nav-button" :disabled="busy" @click="newChat"><Icon name="plus"/>新对话</button><button class="nav-button" :class="{active:view==='chat'}" @click="view='chat';navOpen=false"><Icon name="chat"/>问答</button><button class="nav-button" :class="{active:view==='library'}" @click="view='library';panel=null;navOpen=false;loadLibrary()"><Icon name="file"/>资料库</button></nav>
   <div class="session-heading">最近对话</div><div class="session-list"><div v-for="s in sessions.filter(s=>s.turns.length)" :key="s.id" class="session-row" :class="{selected:s.id===sessionId&&view==='chat'}"><button class="session-open" :disabled="busy" :title="s.title" @click="selectSession(s)">{{s.title}}</button><button class="session-delete" :disabled="busy" :aria-label="`删除对话：${s.title}`" :title="busy?'请先停止回答再删除':'删除对话'" @click="deleteSession(s)"><Icon name="trash"/></button></div></div>
   <div class="sidebar-bottom"><span class="status-dot" :class="{offline:connectionError||!info?.model_ready}"></span>{{connectionLabel}}<button v-if="connectionError||!info?.model_ready" class="icon-button" aria-label="重试连接" :disabled="connectionLoading" @click="refreshConnection"><Icon name="refresh"/></button></div>
  </aside>
  <main class="main-pane">
   <header class="topbar"><button class="icon-button mobile-nav" aria-label="打开导航" @click="navOpen=!navOpen"><Icon name="menu"/></button><span class="page-title">{{title}}</span><div v-if="view==='chat'" class="header-actions"><button class="quiet-button" :class="{enabled:flow}" @click="flow=!flow"><Icon name="flow"/><span>检索流程</span></button></div><button v-else-if="view==='library'" class="primary-button compact" @click="uploadOpen=true"><Icon name="upload"/>上传资料</button></header>
   <div v-if="connectionError" class="connection-banner" role="status"><span>{{connectionError}}</span><button class="quiet-button" :disabled="connectionLoading" @click="refreshConnection">重试连接</button></div>
   <template v-if="view==='chat'">
    <div ref="conversation" class="conversation" @scroll="trackScroll">
     <div v-if="!current?.turns.length" class="empty-chat"><span class="empty-symbol"><Icon name="chat"/></span><h1>有什么制度问题？</h1><div class="suggestions"><button v-for="q in examples" :key="q" :disabled="busy||!info?.model_ready||Boolean(connectionError)" @click="ask(q)">{{q}}<Icon name="arrow"/></button></div></div>
     <div v-else class="turn-list"><section v-for="turn in current.turns" :key="turn.id" class="turn"><div class="user-question">{{turn.question}}</div><div class="assistant-answer">
      <details v-if="flow&&turn.events.length" class="flow-block" open><summary>检索流程<Icon name="chevron"/></summary><div v-for="(e,i) in turn.events" :key="i" class="flow-step"><span class="step-dot"></span><div><strong>{{stageLabel(e)}}</strong><p v-if="e.stage==='routing'">{{e.reason||'进入制度检索'}}</p><p v-else-if="e.stage==='query'">{{queryText(e)}}</p><template v-else-if="e.stage==='vectors'||e.stage==='keywords'"><p>{{e.passage_count||0}} 个候选片段</p><ul v-if="e.titles?.length"><li v-for="(t,j) in e.titles" :key="j">{{t.title||t}}</li></ul></template><template v-else-if="e.stage==='evidence'"><ul v-if="e.titles?.length"><li v-for="(t,j) in e.titles" :key="j">{{t.title||t}}</li></ul><p v-else>未找到匹配条目</p><p v-for="(n,j) in (e.notes||[])" :key="'n'+j">{{n}}</p></template><div v-else-if="e.stage==='prompt'" class="prompt-area"><button v-if="e.messages?.length" class="quiet-button prompt-toggle" @click="e.expanded=!e.expanded">{{e.expanded?'收起完整提示词':'展开完整提示词'}}</button><pre :class="{'complete-prompt':e.expanded}">{{e.expanded?promptFull(e):promptPreview(e)}}</pre></div></div></div></details>
      <MarkdownText v-if="turn.answer" :text="turn.answer" :sources="turn.sources" @cite="answerCite($event,turn)"/><div v-else-if="turn.busy" class="answer-loading"><span class="loading-dot"></span>{{turn.status||'正在查找制度'}}</div>
      <div v-if="turn.error" class="inline-error" role="status"><span>{{turn.error}}</span><button v-if="!turn.busy" class="quiet-button" :disabled="busy" @click="ask(turn.question)">重新提问</button></div>
      <div v-if="turn.sources.length" class="sources"><div class="sources-label">参考来源</div><button v-for="(s,i) in turn.sources" :key="i" :title="s.file" @click="openSource(s)"><span class="source-number">{{s.id}}</span><span class="source-name">{{s.file}}</span><span class="source-page">{{s.article?s.article+' · ':''}}第{{(s.pages||[1]).join('、')}}页</span><Icon name="chevron"/></button></div>
      <div v-if="!turn.busy" class="answer-actions"><button class="icon-button" aria-label="复制回答" @click="copy(turn)"><Icon name="copy"/></button><span v-if="turn.seconds">{{turn.seconds.toFixed(1)}}秒</span></div>
     </div></section></div>
    </div>
    <button v-if="!atBottom&&current?.turns.length" class="jump-latest quiet-button" @click="jumpToLatest">回到最新回答 ↓</button>
    <form class="composer-area" @submit.prevent="ask()"><div class="composer"><textarea ref="composer" v-model="input" rows="2" placeholder="输入你的问题，例如：实验室危化品怎么领用？" aria-label="问题" @keydown="keydown"></textarea><div class="composer-toolbar"><span class="composer-hint">仅依据现行制度回答</span><button v-if="busy" type="button" class="send-button" aria-label="停止回答" @click="controller?.abort()"><Icon name="stop"/></button><button v-else type="submit" class="send-button" aria-label="发送问题" :disabled="!input.trim()||!info?.model_ready||Boolean(connectionError)"><Icon name="send"/></button></div></div></form>
   </template>
   <div v-else class="library-content"><div class="library-toolbar"><h1>资料库</h1><button class="icon-button" aria-label="刷新资料库" :disabled="libraryLoading" @click="loadLibrary"><Icon name="refresh"/></button><label class="search-box"><Icon name="search"/><input v-model="librarySearch" placeholder="查找制度、文号或部门" aria-label="查找制度"></label></div>
    <div v-if="libraryError" class="inline-error" role="status">{{libraryError}}<button class="quiet-button" :disabled="libraryLoading" @click="loadLibrary">重试</button></div>
    <section v-if="tasks.length" class="task-section"><h2>上传记录 <span>{{tasks.length}}</span></h2><div v-for="t in tasks" :key="t.id" class="task-row"><Icon :name="t.status==='done'?'check':'file'"/><div class="task-name">{{taskLabel(t)}}<small v-if="t.phase" class="task-sub">{{t.phase}}</small></div><span class="badge" :class="taskBadgeClass(t)">{{taskStatus(t)}}</span><button v-if="t.status==='failed'&&!t.active" class="quiet-button" :disabled="retryBusy===t.id" @click="retry(t)">{{retryBusy===t.id?'提交中':'重试'}}</button></div><p v-for="t in tasks.filter(t=>t.error)" :key="'e'+t.id" class="inline-error">{{t.error}}</p></section>
    <section class="document-section"><h2>已有制度 <span>{{filteredDocuments.length}}</span></h2><div class="document-table"><div class="document-row table-head"><span>文件</span><span>归口部门</span><span>状态</span></div><button v-for="d in filteredDocuments" :key="d.id" class="document-row" :title="d.title||d.name" @click="openDocument(d)"><span class="document-name"><Icon name="file"/><span class="doc-text"><span class="document-title">{{d.title||d.name||d.id}}</span><span v-if="d.doc_no||d.version_label" class="doc-meta">{{[d.doc_no,d.version_label].filter(Boolean).join(' · ')}}</span></span></span><span class="document-model">{{d.department||'—'}}</span><span class="badge" :class="{success:d.status==='active',processing:d.status==='superseded'}">{{statusLabel(d.status)}}</span></button><div v-if="!filteredDocuments.length" class="empty-list">{{libraryLoading?'正在加载制度':libraryError?'制度暂时无法加载':'没有找到制度'}}</div></div></section>
   </div>
  </main>
  <button v-if="panel" class="pane-backdrop" aria-label="收起资料面板" @click="panel=null"></button>
  <aside v-if="panel" class="detail-pane"><div class="pane-resizer detail-resizer" role="separator" tabindex="0" aria-label="调整资料面板宽度" aria-orientation="vertical" :aria-valuenow="panes.detailWidth.value" aria-valuemin="320" :aria-valuemax="panes.detailMax.value" title="拖动调整宽度，双击恢复" @pointerdown="panes.start($event,'detail')" @keydown="panes.key($event,'detail')" @dblclick="panes.reset('detail')"></div><header><strong>原文资料</strong><button class="icon-button" aria-label="关闭右侧面板" @click="panel=null"><Icon name="close"/></button></header>
   <template v-if="selectedSource"><div class="source-heading"><h2>{{selectedSource.title||selectedSource.file}}</h2><div class="source-tabs"><button :class="{selected:sourceMode==='text'}" @click="sourceMode='text'">证据</button><button v-if="currentHasPdf" :class="{selected:sourceMode==='pdf'}" @click="sourceMode='pdf'">PDF原文</button><a v-if="currentHasPdf" :href="selectedPdf.split('#')[0]+'#page='+pdfPage" target="_blank" rel="noopener">打开原文</a></div></div><PdfPreview v-if="sourceMode==='pdf'&&currentHasPdf" @page="pdfPage=$event" :url="selectedPdf" :pages="documentFor(selectedSource.doc_id)?.pages||selectedSource.pages?.at(-1)||1"/><div v-else class="source-content"><div class="source-page-label" v-if="selectedSource.text">第{{(selectedSource.pages||[1]).join('、')}}页</div><pre v-if="selectedSource.text">{{selectedSource.text}}</pre><p v-else class="panel-empty">该文档没有 PDF 原文，问答回答的“参考来源”中可查看引用的证据文本。</p></div></template>
  </aside>
 </div>
 <div v-if="uploadOpen" class="modal-backdrop" @click.self="!uploading&&(uploadOpen=false)"><section class="modal upload-modal"><header><h2>上传制度资料</h2><button class="icon-button" aria-label="关闭上传" :disabled="uploading" @click="uploadOpen=false"><Icon name="close"/></button></header><div class="drop-zone" @dragover.prevent @drop="drop" @click="!uploading&&fileInput?.click()"><Icon name="upload"/><strong>拖入 PDF、DOCX、MD 或 TXT，或选择文件</strong><input ref="fileInput" type="file" accept=".pdf,.docx,.md,.txt" multiple hidden aria-label="选择制度文件" @change="acceptFiles(($event.target as HTMLInputElement).files)"></div><div v-if="files.length" class="selection-summary">{{files.length}}份文件 · {{(files.reduce((n,f)=>n+f.size,0)/1024/1024).toFixed(1)}}MB</div><div class="selected-files"><div v-for="(f,i) in files" :key="f.name"><Icon name="file"/><span>{{f.name}}</span><button class="icon-button" aria-label="移除文件" :disabled="uploading" @click="files.splice(i,1)"><Icon name="close"/></button></div></div>
  <div class="upload-fields"><label>制度名称 *<input v-model="form.title" placeholder="例如：XX大学实验室安全管理办法" :disabled="uploading"></label><label>文号<input v-model="form.doc_no" placeholder="例如：校发〔2025〕27号" :disabled="uploading"></label><label>版本<input v-model="form.version_label" placeholder="例如：2025年修订版" :disabled="uploading"></label><label>归口部门<input v-model="form.department" placeholder="例如：实验室与设备管理处" :disabled="uploading"></label><label>生效日期<input v-model="form.effective_from" type="date" :disabled="uploading"></label><label>废止日期<input v-model="form.effective_to" type="date" :disabled="uploading"></label></div><div v-if="uploading" class="upload-progress"><span :style="{width:uploadProgress+'%'}"></span></div><footer><button class="quiet-button" :disabled="uploading" @click="uploadOpen=false">取消</button><button class="primary-button" :disabled="uploading||!files.length||!form.title.trim()" @click="upload">{{uploading?`上传中 ${uploadProgress}%`:'上传并入库'}}</button></footer>
 </section></div>
 <div v-if="toast" class="toast" role="status">{{toast}}</div>
</template>

<style scoped>
.brand{align-items:flex-start}
.brand-text{display:flex;flex-direction:column;gap:2px;min-width:0}
.brand-text strong{font-size:17px;line-height:1.2;letter-spacing:.2px}
.brand-text span{font-size:13px;color:var(--muted);line-height:1.2}
.composer-hint{font-size:13px;color:var(--muted);padding-left:3px}
.doc-text{display:flex;flex-direction:column;gap:4px;min-width:0;flex:1;align-items:flex-start}
.document-title{max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.doc-meta{max-width:100%;overflow:hidden;text-overflow:ellipsis;font-size:12px;color:var(--muted);background:#f1f3ee;border-radius:4px;padding:2px 6px;white-space:nowrap}
.task-sub{display:block;font-size:12px;color:var(--muted);margin-top:2px}
.task-section h2 span{font-weight:400;color:var(--muted);margin-left:7px}
</style>
