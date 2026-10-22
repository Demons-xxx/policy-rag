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
type PolicyDocument={id:string;name?:string;title?:string;doc_no?:string|null;version_label?:string|null;department?:string|null;status?:string;pages?:number;has_pdf?:boolean|number;effective_from?:string|null;effective_to?:string|null;supersedes?:string|null;superseded_by?:string|null}
type Task={id:string;files?:string[];name?:string;status?:string;phase?:string;error?:string;active?:boolean;units?:number}

const view=ref<'chat'|'library'>('chat'),navOpen=ref(false),librarySearch=ref(''),libraryStatus=ref(''),libraryDepartment=ref(''),selectedDoc=ref<PolicyDocument|null>(null)
const info=ref<any>(null),documents=ref<PolicyDocument[]>([]),tasks=ref<Task[]>([])
const sessions=ref<Session[]>([]),sessionId=ref(''),input=ref(''),busy=ref(false),flow=ref(false)
const panel=ref<'source'|null>(null),selectedSource=ref<Source|null>(null)
const sourceMode=ref<'text'|'pdf'>('text'),selectedPdf=ref(''),toast=ref(''),conversation=ref<HTMLElement|null>(null),composer=ref<HTMLTextAreaElement|null>(null)
const uploading=ref(false),uploadOpen=ref(false),files=ref<File[]>([]),uploadProgress=ref(0),fileInput=ref<HTMLInputElement|null>(null)
const form=reactive({title:'',doc_no:'',version_label:'',effective_from:'',effective_to:'',department:''})
const libraryLoading=ref(false),libraryError=ref(''),connectionLoading=ref(false),connectionError=ref(''),atBottom=ref(true),retryBusy=ref('')
const pdfPage=ref(1)
let controller:AbortController|null=null,toastTimer:ReturnType<typeof setTimeout>|null=null
const fileMeta=ref<Record<string,Record<string,string>>>({})
let uploadRequest:XMLHttpRequest|null=null,disposed=false,storageWarning=false,libraryRequest:Promise<void>|null=null
const paletteOpen=ref(false),paletteQuery=ref(''),paletteIndex=ref(0),paletteInput=ref<HTMLInputElement|null>(null)
const sidebarCollapsed=ref(localStorage.getItem('policy-rag-sidebar')==='1')
type PaletteItem={icon:string;label:string;hint?:string;run:()=>void}
const paletteCommands=computed<PaletteItem[]>(()=>{
 const items:PaletteItem[]=[
  {icon:'plus',label:'新建对话',hint:'开一个空白会话',run:newChat},
  {icon:'book',label:'打开资料库',hint:'查看与筛选已入库制度',run:()=>{view.value='library';panel.value=null;loadLibrary()}},
  {icon:'upload',label:'上传制度资料',hint:'PDF / DOCX / MD / TXT',run:()=>{uploadOpen.value=true}},
  {icon:'arrow',label:'回到最新回答',hint:'跳到对话底部',run:jumpToLatest},
  {icon:'flow',label:'切换检索流程显示',hint:flow.value?'当前：显示':'当前：隐藏',run:()=>{flow.value=!flow.value}},
  {icon:'refresh',label:'刷新连接与资料库',hint:'重新读取服务状态与文档列表',run:refreshConnection},
 ]
 if(busy.value)items.unshift({icon:'stop',label:'停止本次回答',hint:'Esc',run:()=>controller?.abort()})
 return items
})
const paletteItems=computed<PaletteItem[]>(()=>{
 const q=paletteQuery.value.trim().toLowerCase()
 const commands=paletteCommands.value.filter(c=>!q||c.label.toLowerCase().includes(q)||(c.hint||'').toLowerCase().includes(q))
 const sessionItems=sessions.value.filter(x=>x.turns.length&&(!q||x.title.toLowerCase().includes(q))).slice(0,6).map(x=>({icon:'chat',label:x.title,hint:'切换到该对话',run:()=>selectSession(x)}))
 return [...commands,...sessionItems]
})
function openPalette(){paletteOpen.value=true;paletteQuery.value='';paletteIndex.value=0;nextTick(()=>paletteInput.value?.focus())}
function closePalette(){paletteOpen.value=false}
function runPalette(item:PaletteItem){closePalette();item.run()}
function paletteKey(e:KeyboardEvent){
 if(e.key==='ArrowDown'){e.preventDefault();paletteIndex.value=Math.min(paletteIndex.value+1,paletteItems.value.length-1)}
 else if(e.key==='ArrowUp'){e.preventDefault();paletteIndex.value=Math.max(paletteIndex.value-1,0)}
 else if(e.key==='Enter'){e.preventDefault();const item=paletteItems.value[paletteIndex.value];if(item)runPalette(item)}
 else if(e.key==='Escape'){e.preventDefault();closePalette()}
}
function toggleSidebar(){sidebarCollapsed.value=!sidebarCollapsed.value;try{localStorage.setItem('policy-rag-sidebar',sidebarCollapsed.value?'1':'0')}catch{}}
const panes=usePaneWidths(()=>Boolean(panel.value))
const connectionLabel=computed(()=>connectionLoading.value?'正在连接':connectionError.value?'连接已断开':info.value?.model_ready?'服务已连接':'服务准备中')
const current=computed(()=>sessions.value.find(s=>s.id===sessionId.value))
const title=computed(()=>view.value==='library'?'资料库':current.value?.title||'新对话')
const departments=computed(()=>Array.from(new Set(documents.value.map(d=>d.department).filter(Boolean))).sort() as string[])
const filteredDocuments=computed(()=>{const text=librarySearch.value.trim().toLowerCase();return documents.value.filter(d=>{if(libraryStatus.value&&(d.status||'active')!==libraryStatus.value)return false;if(libraryDepartment.value&&d.department!==libraryDepartment.value)return false;return `${d.title||''} ${d.name||''} ${d.doc_no||''} ${d.version_label||''} ${d.department||''}`.toLowerCase().includes(text)})})
const selectedDocTitle=computed(()=>{const id=selectedDoc.value?.id;if(!id)return '';return documents.value.find(d=>d.id===id)?.title||''})
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
 selectedDoc.value=null
 selectedSource.value=source
 pdfPage.value=source.focus_page&&source.pages?.includes(source.focus_page)?source.focus_page:source.pages?.[0]||1
 selectedPdf.value=`/api/documents/${encodeURIComponent(source.doc_id)}/pdf#page=${pdfPage.value}`
 sourceMode.value='text';panel.value='source'
}
function openDocument(d:PolicyDocument){
 selectedDoc.value=d
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
 view.value='chat';navOpen.value=false;input.value='';busy.value=true;nextTick(()=>composer.value?.focus())
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
  else if(e.stage==='revision'){flush();turn.answer=e.text}
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
async function regenerate(turn:Turn){
 if(busy.value||!current.value)return
 const session=current.value,len=turnsIndex(session,turn)
 if(len<0)return
 session.turns.splice(len)  // 去掉该轮及其后内容，用剩余历史重新生成
 view.value='chat';panel.value=null;await ask(turn.question)
}
function turnsIndex(session:Session,turn:Turn){return session.turns.findIndex(t=>t.id===turn.id)}
function selectedDocTitleOf(id?:string|null){if(!id)return '';return documents.value.find(d=>d.id===id)?.title||id}
function exportTurn(turn:Turn){
 const lines=[`# ${turn.question}`,'',turn.answer||'（回答为空）','','## 参考来源']
 turn.sources.forEach(s=>{const page=(s.pages||[1]).join('、');lines.push(`- [${s.id}] ${s.file}${s.article?' · '+s.article:''}（第${page}页）`)})
 if(!turn.sources.length)lines.push('- （无）')
 const blob=new Blob([lines.join('\n')],{type:'text/markdown;charset=utf-8'})
 const url=URL.createObjectURL(blob),a=document.createElement('a')
 a.href=url;a.download=`回答-${turn.question.slice(0,20).replace(/[\/:*?"<>|]/g,'_')||'导出'}.md`
 document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)
 notice('已导出 Markdown')
}
function renameSession(session:Session){
 if(busy.value)return
 const next=window.prompt('修改对话名称（1–40 字）',session.title)
 if(next===null)return
 const title=next.trim().slice(0,40)
 if(!title){notice('名称不能为空');return}
 session.title=title;persist();notice('已重命名')
}
async function setDocStatus(doc:PolicyDocument|null,status:'repealed'|'active'){
 if(!doc||busy.value)return
 const target=doc.title||doc.name||doc.id
 if(status==='repealed'&&!window.confirm(`停用《${target}》？
停用后不再参与问答，可在同一位置恢复。`))return
 try{
  if(status==='repealed')await api(`/api/documents/${encodeURIComponent(doc.id)}?publish=false`,{method:'DELETE'})
  else await api(`/api/documents/${encodeURIComponent(doc.id)}/restore?publish=false`,{method:'POST'})
  notice(status==='repealed'?'已停用该文档':'已恢复该文档')
  await loadLibrary()
  selectedDoc.value=documents.value.find(item=>item.id===doc.id)||{...doc,status}
 }catch(e:any){notice(e.message)}
}
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
function fileKey(f:File){return f.name+'|'+f.size}
function fileMetaFor(f:File){const k=fileKey(f);if(!fileMeta.value[k])fileMeta.value[k]={title:'',doc_no:'',version_label:'',effective_from:'',effective_to:'',department:''};return fileMeta.value[k]}
function uploadMetadata(){const meta:Record<string,string>={};const title=form.title.trim();if(title)meta.title=title;if(form.doc_no.trim())meta.doc_no=form.doc_no.trim();if(form.version_label.trim())meta.version_label=form.version_label.trim();if(form.effective_from)meta.effective_from=form.effective_from;if(form.effective_to)meta.effective_to=form.effective_to;if(form.department.trim())meta.department=form.department.trim();return meta}
function metaForFile(f:File){const base=uploadMetadata()||{};const own=fileMeta.value[fileKey(f)]||{};const merged:Record<string,string>={...base};for(const key of Object.keys(own)){const text=String(own[key]||'').trim();if(text)merged[key]=text}return merged}
function metaPayload(){return files.value.length>1?files.value.map(metaForFile):(uploadMetadata()||{})}
function missingTitle(){if(!files.value.length)return true;return files.value.some(f=>!String(metaForFile(f).title||'').trim())}
async function upload(){
 if(uploading.value)return
 const meta=metaPayload()
 if(missingTitle()){notice('请为每份文件填写制度名称');return}
 const data=new FormData();files.value.forEach(f=>data.append('files',f));data.append('metadata',JSON.stringify(meta))
 uploading.value=true;uploadProgress.value=0
 try{await new Promise<void>((resolve,reject)=>{const xhr=new XMLHttpRequest();uploadRequest=xhr;xhr.open('POST','/api/ingest');xhr.timeout=600000;xhr.upload.onprogress=e=>{if(e.lengthComputable)uploadProgress.value=Math.round(e.loaded/e.total*100)};xhr.onload=()=>{let result:any={};try{result=JSON.parse(xhr.responseText)}catch{}xhr.status>=200&&xhr.status<300?resolve():reject(new Error(typeof result.detail==='string'?result.detail:'上传未完成'))};xhr.onerror=()=>reject(new Error('连接中断，文件已保留，可重新上传'));xhr.ontimeout=()=>reject(new Error('上传超时，文件已保留'));xhr.onabort=()=>reject(new Error('上传已取消'));xhr.send(data)})
  files.value=[];uploadOpen.value=false;Object.assign(form,{title:'',doc_no:'',version_label:'',effective_from:'',effective_to:'',department:''});notice('资料已加入处理队列');await loadLibrary()
 }catch(e:any){notice(e.message)}finally{uploading.value=false;uploadRequest=null}
}
async function retry(task:Task){if(retryBusy.value||task.active)return;retryBusy.value=task.id;try{await api(`/api/ingest/${task.id}/retry`,{method:'POST'});await loadLibrary();notice('任务已重新加入处理队列')}catch(e:any){notice(e.message)}finally{retryBusy.value=''}}
function keydown(event:KeyboardEvent){if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();ask()}}
watch(input,()=>nextTick(()=>{if(composer.value){composer.value.style.height='auto';composer.value.style.height=Math.min(composer.value.scrollHeight,180)+'px'}}))
function shortcuts(event:KeyboardEvent){
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();paletteOpen.value?closePalette():openPalette();return}
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='b'){event.preventDefault();toggleSidebar();return}
 if(event.key!=='Escape'||event.isComposing)return
 if(paletteOpen.value){closePalette();return}
 if(uploadOpen.value&&!uploading.value){uploadOpen.value=false;return}
 if(busy.value){controller?.abort();return}
 if(panel.value){panel.value=null;return}
 navOpen.value=false
}
onMounted(async()=>{
 try{const saved=JSON.parse(localStorage.getItem('policy-rag-sessions')||'[]');if(Array.isArray(saved))sessions.value=saved.filter(s=>typeof s?.id==='string'&&typeof s?.title==='string'&&Array.isArray(s.turns)).slice(0,15).map(s=>({...s,turns:s.turns.filter((t:any)=>typeof t?.id==='string'&&typeof t.question==='string'&&typeof t.answer==='string').map((t:any)=>({...t,busy:false,error:t.busy?'上次回答尚未完成':t.error,sources:Array.isArray(t.sources)?t.sources.filter((v:any)=>typeof v?.doc_id==='string'&&typeof v.file==='string').map((v:any)=>({...v,pages:Array.isArray(v.pages)?v.pages.filter((p:any)=>Number.isInteger(p)&&p>0):[1]})):[],events:Array.isArray(t.events)?t.events.filter((e:any)=>typeof e?.stage==='string'&&(!e.messages||Array.isArray(e.messages))):[]}))}))
 const last=localStorage.getItem('policy-rag-session-id');const restored=sessions.value.find(s=>s.id===last)||sessions.value.find(s=>s.turns.length);if(restored){sessionId.value=restored.id;nextTick(jumpToLatest)}else newChat()
 }catch{newChat();notice('历史记录未能读取，已打开新对话')}
 document.addEventListener('visibilitychange',visibilityChanged);window.addEventListener('keydown',shortcuts);await Promise.all([loadInfo(),loadLibrary()])
})
onBeforeUnmount(()=>{disposed=true;controller?.abort();uploadRequest?.abort();document.removeEventListener('visibilitychange',visibilityChanged);window.removeEventListener('keydown',shortcuts);if(toastTimer)clearTimeout(toastTimer)})
</script>

<template>
 <div class="workspace" :class="{'has-panel':panel,'nav-open':navOpen,'resizing':panes.dragging.value,'sidebar-collapsed':sidebarCollapsed}" :style="{'--sidebar-width':panes.sidebarWidth.value+'px','--detail-width':panes.detailWidth.value+'px'}">
  <button v-if="navOpen" class="nav-backdrop" aria-label="关闭导航" @click="navOpen=false"></button>
  <aside class="sidebar">
   <div class="pane-resizer sidebar-resizer" role="separator" tabindex="0" aria-label="调整导航宽度" aria-orientation="vertical" :aria-valuenow="panes.sidebarWidth.value" aria-valuemin="180" :aria-valuemax="panes.sidebarMax.value" title="拖动调整宽度，双击恢复" @pointerdown="panes.start($event,'sidebar')" @keydown="panes.key($event,'sidebar')" @dblclick="panes.reset('sidebar')"></div>
   <div class="brand" aria-label="PolicyRAG 政策制度问答"><span class="brand-mark"><span></span><span></span><span></span><span></span></span><div class="brand-text"><strong>PolicyRAG</strong><span>政策制度问答</span></div><button class="sidebar-toggle" :aria-label="sidebarCollapsed?'展开侧栏':'收起侧栏'" :title="(sidebarCollapsed?'展开':'收起')+'侧栏 (Ctrl B)'" @click="toggleSidebar"><Icon name="panel"/></button></div>
   <nav><button class="nav-button" :disabled="busy" title="新对话 (Ctrl K → 新建对话)" @click="newChat"><Icon name="plus"/><span class="nav-label">新对话</span></button><button class="nav-button" :class="{active:view==='chat'}" title="问答" @click="view='chat';navOpen=false"><Icon name="chat"/><span class="nav-label">问答</span></button><button class="nav-button" :class="{active:view==='library'}" title="资料库" @click="view='library';panel=null;navOpen=false;loadLibrary()"><Icon name="file"/><span class="nav-label">资料库</span></button></nav>
   <div class="session-heading">最近对话</div><div class="session-list"><div v-for="s in sessions.filter(s=>s.turns.length)" :key="s.id" class="session-row" :class="{selected:s.id===sessionId&&view==='chat'}"><button class="session-open" :disabled="busy" :title="s.title" @click="selectSession(s)">{{s.title}}</button><button class="session-rename" :disabled="busy" :aria-label="`重命名对话：${s.title}`" :title="busy?'请先停止回答再重命名':'重命名对话'" @click="renameSession(s)"><Icon name="edit"/></button><button class="session-delete" :disabled="busy" :aria-label="`删除对话：${s.title}`" :title="busy?'请先停止回答再删除':'删除对话'" @click="deleteSession(s)"><Icon name="trash"/></button></div></div>
   <div class="sidebar-bottom"><span class="status-dot" :class="{offline:connectionError||!info?.model_ready}"></span><span class="status-text">{{connectionLabel}}</span><button v-if="connectionError||!info?.model_ready" class="icon-button" aria-label="重试连接" :disabled="connectionLoading" @click="refreshConnection"><Icon name="refresh"/></button></div>
  </aside>
  <main class="main-pane">
   <header class="topbar"><button class="icon-button mobile-nav" aria-label="打开导航" @click="navOpen=!navOpen"><Icon name="menu"/></button><span class="page-title">{{title}}</span><button class="icon-button palette-trigger" title="命令面板 (Ctrl K)" aria-label="打开命令面板" @click="openPalette"><Icon name="search"/></button><div v-if="view==='chat'" class="header-actions"><button class="quiet-button" :class="{enabled:flow}" @click="flow=!flow"><Icon name="flow"/><span>检索流程</span></button></div><button v-else-if="view==='library'" class="primary-button compact" @click="uploadOpen=true"><Icon name="upload"/>上传资料</button></header>
   <div v-if="connectionError" class="connection-banner" role="status"><span>{{connectionError}}</span><button class="quiet-button" :disabled="connectionLoading" @click="refreshConnection">重试连接</button></div>
   <template v-if="view==='chat'">
    <div ref="conversation" class="conversation" @scroll="trackScroll">
     <div v-if="!current?.turns.length" class="empty-chat"><span class="empty-symbol"><Icon name="chat"/></span><h1>有什么制度问题？</h1><div class="suggestions"><button v-for="q in examples" :key="q" :disabled="busy||!info?.model_ready||Boolean(connectionError)" @click="ask(q)">{{q}}<Icon name="arrow"/></button></div><p class="shortcut-hint">Enter 发送 · Shift+Enter 换行 · Ctrl K 命令面板 · Esc 停止生成</p></div>
     <div v-else class="turn-list"><section v-for="turn in current.turns" :key="turn.id" class="turn"><div class="user-question">{{turn.question}}</div><div class="assistant-answer">
      <details v-if="flow&&turn.events.length" class="flow-block" open><summary>检索流程<Icon name="chevron"/></summary><div v-for="(e,i) in turn.events" :key="i" class="flow-step"><span class="step-dot"></span><div><strong>{{stageLabel(e)}}</strong><p v-if="e.stage==='routing'">{{e.reason||'进入制度检索'}}</p><p v-else-if="e.stage==='query'">{{queryText(e)}}</p><template v-else-if="e.stage==='vectors'||e.stage==='keywords'"><p>{{e.passage_count||0}} 个候选片段</p><ul v-if="e.titles?.length"><li v-for="(t,j) in e.titles" :key="j">{{t.title||t}}</li></ul></template><template v-else-if="e.stage==='evidence'"><ul v-if="e.titles?.length"><li v-for="(t,j) in e.titles" :key="j">{{t.title||t}}</li></ul><p v-else>未找到匹配条目</p><p v-for="(n,j) in (e.notes||[])" :key="'n'+j">{{n}}</p></template><div v-else-if="e.stage==='prompt'" class="prompt-area"><button v-if="e.messages?.length" class="quiet-button prompt-toggle" @click="e.expanded=!e.expanded">{{e.expanded?'收起完整提示词':'展开完整提示词'}}</button><pre :class="{'complete-prompt':e.expanded}">{{e.expanded?promptFull(e):promptPreview(e)}}</pre></div></div></div></details>
      <div v-if="turn.answer" class="answer-body" :class="{streaming:turn.busy}"><MarkdownText :text="turn.answer" :sources="turn.sources" @cite="answerCite($event,turn)"/></div><div v-else-if="turn.busy" class="answer-loading"><span class="loading-dot"></span>{{turn.status||'正在查找制度'}}</div>
      <div v-if="turn.error" class="inline-error" role="status"><span>{{turn.error}}</span><button v-if="!turn.busy" class="quiet-button" :disabled="busy" @click="ask(turn.question)">重新提问</button></div>
      <div v-if="turn.sources.length" class="sources"><div class="sources-label">参考来源</div><button v-for="(s,i) in turn.sources" :key="i" :title="s.file" @click="openSource(s)"><span class="source-number">{{s.id}}</span><span class="source-name">{{s.file}}</span><span class="source-page">{{s.article?s.article+' · ':''}}第{{(s.pages||[1]).join('、')}}页</span><Icon name="chevron"/></button></div>
      <div v-if="turn.busy" class="streaming-pill"><span class="streaming-dot"></span>{{turn.status||'正在生成回答'}}</div><div v-if="!turn.busy" class="answer-actions"><button class="icon-button" aria-label="重新生成" title="用同一上下文重新生成" :disabled="busy" @click="regenerate(turn)"><Icon name="refresh"/></button><button class="icon-button" aria-label="导出 Markdown" title="导出问题/回答/来源" @click="exportTurn(turn)"><Icon name="download"/></button><button class="icon-button" aria-label="复制回答" @click="copy(turn)"><Icon name="copy"/></button><span v-if="turn.seconds">{{turn.seconds.toFixed(1)}}秒</span></div>
     </div></section></div>
    </div>
    <button v-if="!atBottom&&current?.turns.length" class="jump-latest quiet-button" @click="jumpToLatest">{{busy?'有新内容 · 回到最新':'回到最新回答 ↓'}}</button>
    <form class="composer-area" @submit.prevent="ask()"><div class="composer"><textarea ref="composer" v-model="input" rows="2" placeholder="输入你的问题，例如：实验室危化品怎么领用？" aria-label="问题" @keydown="keydown"></textarea><div class="composer-toolbar"><span class="composer-hint">{{busy?'Esc 可停止生成':'仅依据现行制度回答'}}</span><button v-if="busy" type="button" class="send-button" aria-label="停止回答" @click="controller?.abort()"><Icon name="stop"/></button><button v-else type="submit" class="send-button" aria-label="发送问题" :disabled="!input.trim()||!info?.model_ready||Boolean(connectionError)"><Icon name="send"/></button></div></div></form>
   </template>
   <div v-else class="library-content"><div class="library-toolbar"><h1>资料库</h1><button class="icon-button" aria-label="刷新资料库" :disabled="libraryLoading" @click="loadLibrary"><Icon name="refresh"/></button><label class="search-box"><Icon name="search"/><input v-model="librarySearch" placeholder="查找制度、文号或部门" aria-label="查找制度"></label><select v-model="libraryStatus" class="filter-select" aria-label="按状态筛选"><option value="">全部状态</option><option value="active">现行</option><option value="superseded">已被替代</option><option value="repealed">已废止</option></select><select v-model="libraryDepartment" class="filter-select" aria-label="按部门筛选"><option value="">全部部门</option><option v-for="dep in departments" :key="dep" :value="dep">{{dep}}</option></select></div>
    <div v-if="libraryError" class="inline-error" role="status">{{libraryError}}<button class="quiet-button" :disabled="libraryLoading" @click="loadLibrary">重试</button></div>
    <section v-if="tasks.length" class="task-section"><h2>上传记录 <span>{{tasks.length}}</span></h2><div v-for="t in tasks" :key="t.id" class="task-row"><Icon :name="t.status==='done'?'check':'file'"/><div class="task-name">{{taskLabel(t)}}<small v-if="t.phase" class="task-sub">{{t.phase}}</small></div><span class="badge" :class="taskBadgeClass(t)">{{taskStatus(t)}}</span><button v-if="t.status==='failed'&&!t.active" class="quiet-button" :disabled="retryBusy===t.id" @click="retry(t)">{{retryBusy===t.id?'提交中':'重试'}}</button></div><p v-for="t in tasks.filter(t=>t.error)" :key="'e'+t.id" class="inline-error">{{t.error}}</p></section>
    <section class="document-section"><h2>已有制度 <span>{{filteredDocuments.length}}</span></h2><div class="document-table"><div class="document-row table-head"><span>文件</span><span>归口部门</span><span>状态</span></div><button v-for="d in filteredDocuments" :key="d.id" class="document-row" :title="d.title||d.name" @click="openDocument(d)"><span class="document-name"><Icon name="file"/><span class="doc-text"><span class="document-title">{{d.title||d.name||d.id}}</span><span v-if="d.doc_no||d.version_label" class="doc-meta">{{[d.doc_no,d.version_label].filter(Boolean).join(' · ')}}</span></span></span><span class="document-model">{{d.department||'—'}}</span><span class="badge" :class="{success:d.status==='active',processing:d.status==='superseded'}">{{statusLabel(d.status)}}</span></button><div v-if="!filteredDocuments.length" class="empty-list">{{libraryLoading?'正在加载制度':libraryError?'制度暂时无法加载':'没有找到制度'}}</div></div></section>
   </div>
  </main>
  <button v-if="panel" class="pane-backdrop" aria-label="收起资料面板" @click="panel=null"></button>
  <aside v-if="panel" class="detail-pane"><div class="pane-resizer detail-resizer" role="separator" tabindex="0" aria-label="调整资料面板宽度" aria-orientation="vertical" :aria-valuenow="panes.detailWidth.value" aria-valuemin="320" :aria-valuemax="panes.detailMax.value" title="拖动调整宽度，双击恢复" @pointerdown="panes.start($event,'detail')" @keydown="panes.key($event,'detail')" @dblclick="panes.reset('detail')"></div><header><strong>原文资料</strong><button class="icon-button" aria-label="关闭右侧面板" @click="panel=null"><Icon name="close"/></button></header>
   <template v-if="selectedSource"><div v-if="selectedDoc" class="doc-detail"><div class="doc-detail-row"><span>状态</span><span class="badge" :class="{success:selectedDoc.status==='active',processing:selectedDoc.status==='superseded'}">{{statusLabel(selectedDoc.status)}}</span></div><div class="doc-detail-row"><span>归口部门</span><span>{{selectedDoc.department||'—'}}</span></div><div class="doc-detail-row"><span>生效</span><span>{{selectedDoc.effective_from||'—'}}{{selectedDoc.effective_to?' 至 '+selectedDoc.effective_to:''}}</span></div><div v-if="selectedDoc.doc_no||selectedDoc.version_label" class="doc-detail-row"><span>文号/版本</span><span>{{[selectedDoc.doc_no,selectedDoc.version_label].filter(Boolean).join(' · ')}}</span></div><div v-if="selectedDoc.supersedes||selectedDoc.superseded_by" class="doc-detail-row"><span>版本链</span><span>{{selectedDoc.superseded_by?'已被替代 → '+selectedDocTitleOf(selectedDoc.superseded_by):''}}{{selectedDoc.supersedes?' 本版替代 → '+selectedDocTitleOf(selectedDoc.supersedes):''}}</span></div><div class="doc-detail-actions"><button v-if="selectedDoc.status!=='repealed'" class="quiet-button" :disabled="busy" @click="setDocStatus(selectedDoc,'repealed')">停用该文档</button><button v-else class="quiet-button" :disabled="busy" @click="setDocStatus(selectedDoc,'active')">恢复文档</button></div></div><div class="source-heading"><h2>{{selectedSource.title||selectedSource.file}}</h2><div class="source-tabs"><button :class="{selected:sourceMode==='text'}" @click="sourceMode='text'">证据</button><button v-if="currentHasPdf" :class="{selected:sourceMode==='pdf'}" @click="sourceMode='pdf'">PDF原文</button><a v-if="currentHasPdf" :href="selectedPdf.split('#')[0]+'#page='+pdfPage" target="_blank" rel="noopener">打开原文</a></div></div><PdfPreview v-if="sourceMode==='pdf'&&currentHasPdf" @page="pdfPage=$event" :url="selectedPdf" :pages="documentFor(selectedSource.doc_id)?.pages||selectedSource.pages?.at(-1)||1"/><div v-else class="source-content"><div class="source-page-label" v-if="selectedSource.text">第{{(selectedSource.pages||[1]).join('、')}}页</div><pre v-if="selectedSource.text">{{selectedSource.text}}</pre><p v-else class="panel-empty">该文档没有 PDF 原文，问答回答的“参考来源”中可查看引用的证据文本。</p></div></template>
  </aside>
 </div>
 <div v-if="uploadOpen" class="modal-backdrop" @click.self="!uploading&&(uploadOpen=false)"><section class="modal upload-modal"><header><h2>上传制度资料</h2><button class="icon-button" aria-label="关闭上传" :disabled="uploading" @click="uploadOpen=false"><Icon name="close"/></button></header><div class="drop-zone" @dragover.prevent @drop="drop" @click="!uploading&&fileInput?.click()"><Icon name="upload"/><strong>拖入 PDF、DOCX、MD 或 TXT，或选择文件</strong><input ref="fileInput" type="file" accept=".pdf,.docx,.md,.txt" multiple hidden aria-label="选择制度文件" @change="acceptFiles(($event.target as HTMLInputElement).files)"></div><div v-if="files.length" class="selection-summary">{{files.length}}份文件 · {{(files.reduce((n,f)=>n+f.size,0)/1024/1024).toFixed(1)}}MB</div><div class="selected-files"><div v-for="(f,i) in files" :key="f.name"><Icon name="file"/><span>{{f.name}}</span><button class="icon-button" aria-label="移除文件" :disabled="uploading" @click="files.splice(i,1)"><Icon name="close"/></button></div></div>
  <div class="upload-fields"><label>制度名称 *<input v-model="form.title" placeholder="例如：XX大学实验室安全管理办法" :disabled="uploading"></label><label>文号<input v-model="form.doc_no" placeholder="例如：校发〔2025〕27号" :disabled="uploading"></label><label>版本<input v-model="form.version_label" placeholder="例如：2025年修订版" :disabled="uploading"></label><label>归口部门<input v-model="form.department" placeholder="例如：实验室与设备管理处" :disabled="uploading"></label><label>生效日期<input v-model="form.effective_from" type="date" :disabled="uploading"></label><label>废止日期<input v-model="form.effective_to" type="date" :disabled="uploading"></label></div><div v-if="files.length>1" class="upload-fields per-file"><strong class="per-file-hint">已选择多份文件，请为每份填写制度名称（其余字段沿用上方填写值）</strong><div v-for="f in files" :key="fileKey(f)" class="per-file-row"><span class="per-file-name">{{f.name}}</span><label>制度名称 *<input v-model="fileMetaFor(f).title" placeholder="该文件的制度名称" :disabled="uploading"></label><label>文号<input v-model="fileMetaFor(f).doc_no" placeholder="可留空" :disabled="uploading"></label></div></div><div v-if="uploading" class="upload-progress"><span :style="{width:uploadProgress+'%'}"></span></div><footer><button class="quiet-button" :disabled="uploading" @click="uploadOpen=false">取消</button><button class="primary-button" :disabled="uploading||!files.length||missingTitle()" @click="upload">{{uploading?`上传中 ${uploadProgress}%`:'上传并入库'}}</button></footer>
 </section></div>
 <Transition name="palette">
  <div v-if="paletteOpen" class="palette-backdrop" @click.self="closePalette">
   <section class="palette" role="dialog" aria-modal="true" aria-label="命令面板">
    <div class="palette-search"><Icon name="search"/><input ref="paletteInput" v-model="paletteQuery" placeholder="搜索对话或执行操作…" aria-label="搜索对话或执行操作" @keydown="paletteKey"></div>
    <div class="palette-list" role="listbox">
     <button v-for="(item,i) in paletteItems" :key="i" class="palette-item" :class="{active:i===paletteIndex}" role="option" :aria-selected="i===paletteIndex" @mouseenter="paletteIndex=i" @click="runPalette(item)"><Icon :name="item.icon"/><span class="palette-label">{{item.label}}</span><span v-if="item.hint" class="palette-hint">{{item.hint}}</span></button>
     <p v-if="!paletteItems.length" class="palette-empty">没有匹配的命令或对话</p>
    </div>
    <footer class="palette-foot"><span>↑↓ 选择</span><span>Enter 执行</span><span>Esc 关闭</span></footer>
   </section>
  </div>
 </Transition>
<div v-if="toast" class="toast" role="status">{{toast}}</div>
</template>

<style scoped>
.per-file{display:flex;flex-direction:column;gap:.5rem}.per-file-hint{font-size:.85rem;color:var(--muted,#666);font-weight:500}.per-file-row{display:grid;grid-template-columns:minmax(8rem,1fr) minmax(10rem,1.2fr) minmax(8rem,1fr);gap:.5rem;align-items:end;padding:.5rem 0;border-bottom:1px dashed rgba(0,0,0,.12)}.per-file-name{font-size:.85rem;word-break:break-all;color:var(--muted,#666)}
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
.filter-select{height:32px;border:1px solid var(--line,#d8d9d0);border-radius:8px;background:#fff;color:var(--ink,#222);font-size:13px;padding:0 8px}
.session-rename{width:26px;height:26px;opacity:0;background:none;border:0;color:var(--muted);cursor:pointer;border-radius:6px;padding:4px}
.session-row:hover .session-rename,.session-row:focus-within .session-rename{opacity:1}
.session-rename:hover{background:#ecefe7;color:var(--ink)}
.doc-detail{display:flex;flex-direction:column;gap:6px;padding:10px 12px;border-bottom:1px solid #ecece4;background:#fafbf7}
.doc-detail-row{display:flex;justify-content:space-between;gap:10px;font-size:13px}
.doc-detail-row>span:first-child{color:var(--muted);flex:0 0 auto}
.doc-detail-row>span:last-child{text-align:right;word-break:break-all}
.doc-detail-actions{display:flex;gap:8px;padding-top:4px}

/* ——— 交互润色（T24）：命令面板 / 侧栏折叠 / 流式状态 / 轻动效 ——— */
.sidebar-toggle{display:flex;align-items:center;justify-content:center;width:30px;height:30px;margin-left:auto;flex-shrink:0;border-radius:8px;color:var(--muted);background:none}
.sidebar-toggle:hover{background:#e9ece7;color:#25312f}
.palette-trigger{flex-shrink:0;color:var(--muted)}
.palette-trigger:hover{background:#e9ece7;color:#25312f}
.palette-backdrop{position:fixed;inset:0;background:#1c2a221f;display:flex;align-items:flex-start;justify-content:center;padding:12vh 16px 16px;z-index:60}
.palette{width:min(560px,100%);background:#fff;border:1px solid #e2e7df;border-radius:14px;box-shadow:0 24px 70px #1f33261f;overflow:hidden;display:flex;flex-direction:column;max-height:70dvh}
.palette-search{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid #ecefe8;color:var(--muted)}
.palette-search input{flex:1;border:0;background:none;font-size:15px;min-width:0}
.palette-list{overflow:auto;padding:6px;min-height:0}
.palette-item{display:flex;align-items:center;gap:10px;width:100%;padding:10px;border-radius:9px;text-align:left;font-size:14px;color:#25312f}
.palette-item.active{background:#eef3ec}
.palette-item svg{color:var(--muted)}
.palette-label{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.palette-hint{font-size:12px;color:var(--muted);flex-shrink:0;max-width:46%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.palette-empty{padding:18px;text-align:center;color:var(--muted);font-size:13px}
.palette-foot{display:flex;gap:16px;padding:9px 16px;border-top:1px solid #ecefe8;color:var(--muted);font-size:12px}
.palette-enter-active,.palette-leave-active{transition:opacity .15s ease-out}
.palette-enter-active .palette,.palette-leave-active .palette{transition:transform .15s ease-out}
.palette-enter-from,.palette-leave-to{opacity:0}
.palette-enter-from .palette,.palette-leave-to .palette{transform:translateY(-6px) scale(.99)}
.answer-body.streaming .prose>:last-child:not(ul):not(ol)::after,.answer-body.streaming .prose>:last-child>li:last-child::after{content:"";display:inline-block;width:7px;height:.95em;margin-left:4px;background:var(--accent);border-radius:2px;vertical-align:-2px;animation:caret 1s steps(2,start) infinite}
@keyframes caret{50%{opacity:0}}
.streaming-pill{display:inline-flex;align-items:center;gap:8px;font-size:13px;color:var(--muted);padding:6px 0}
.streaming-dot{width:7px;height:7px;border-radius:50%;background:var(--accent);animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{50%{opacity:.25;transform:scale(.8)}}
.answer-actions{transition:opacity .16s ease-out}
.shortcut-hint{margin:18px auto 0;font-size:12px;color:var(--muted);text-align:center;line-height:1.7}
@media(hover:hover){.turn .answer-actions{opacity:0}.turn:hover .answer-actions,.turn:focus-within .answer-actions{opacity:1}}
@media(min-width:901px){
 .workspace.sidebar-collapsed{grid-template-columns:76px minmax(0,1fr)}
 .workspace.sidebar-collapsed.has-panel{grid-template-columns:76px minmax(0,1fr) var(--detail-width,420px)}
 .sidebar-collapsed .sidebar{padding:22px 8px 14px}
 .sidebar-collapsed .brand{padding:0 0 20px;justify-content:center}
 .sidebar-collapsed .brand-mark,.sidebar-collapsed .brand-text,.sidebar-collapsed .nav-label,.sidebar-collapsed .session-heading,.sidebar-collapsed .session-list{display:none}
 .sidebar-collapsed .sidebar-toggle{margin:0 auto}
 .sidebar-collapsed .nav-button{justify-content:center;padding:12px 0}
 .sidebar-collapsed .sidebar-bottom{justify-content:center}
 .sidebar-collapsed .status-text,.sidebar-collapsed .sidebar-bottom button{display:none}
}
@media(prefers-reduced-motion:reduce){.streaming-dot{animation:none}.answer-body.streaming .prose>:last-child:not(ul):not(ol)::after,.answer-body.streaming .prose>:last-child>li:last-child::after{animation:none}.palette-enter-active,.palette-leave-active,.palette-enter-active .palette,.palette-leave-active .palette{transition:none}}
</style>
