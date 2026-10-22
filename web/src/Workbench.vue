<script setup lang="ts">
import {ref,computed,watch} from 'vue'
import Icon from './Icon.vue'
import PdfPreview from './PdfPreview.vue'

type Source={id:number;doc_id:string;file:string;pages:number[];focus_page?:number|null;title?:string;text:string;article?:string|null;chapter?:string|null;doc_no?:string|null;version_label?:string|null}
type PolicyDocument={id:string;name?:string;title?:string;doc_no?:string|null;version_label?:string|null;department?:string|null;status?:string;pages?:number;has_pdf?:boolean|number;effective_from?:string|null;effective_to?:string|null;supersedes?:string|null;superseded_by?:string|null}
type Unit={id:string;kind:string;chapter:string;section:string;article:string;pages:number[];text:string}
type Row={label:string;left?:Unit;right?:Unit;status:'same'|'changed'|'onlyLeft'|'onlyRight'}
type Diff={prefix:string;left:string;right:string;suffix:string}

const props=defineProps<{source:Source|null;doc:PolicyDocument|null;docs:PolicyDocument[];question?:string;answer?:string;sources?:Source[];busy?:boolean}>()
const emit=defineEmits<{(e:'close'):void;(e:'pdfPage',page:number):void;(e:'toggleStatus',doc:PolicyDocument):void}>()

const tab=ref<'text'|'compare'|'export'>('text')
const pdfMode=ref(false)
const units=ref<Unit[]>([]),unitsLoading=ref(false),unitsError=ref('')
const otherId=ref(''),otherUnits=ref<Unit[]>([]),otherLoading=ref(false),otherError=ref('')
const onlyDiff=ref(false),copied=ref('')
const unitCache=new Map<string,Unit[]>()

const docId=computed(()=>props.doc?.id||props.source?.doc_id||'')
const hasPdf=computed(()=>{if(!props.doc)return /\.pdf$/i.test(props.source?.file||'');return props.doc.has_pdf===true||props.doc.has_pdf===1})
const pages=computed(()=>props.doc?.pages||Math.max(...(props.source?.pages||[1]),1))
const initialPage=computed(()=>{const s=props.source;return s&&s.focus_page&&s.pages?.includes(s.focus_page)?s.focus_page:s?.pages?.[0]||1})
const pdfUrl=computed(()=>`/api/documents/${encodeURIComponent(docId.value)}/pdf#page=${initialPage.value}`)
const comparable=computed(()=>Boolean(props.doc&&(props.doc.supersedes||props.doc.superseded_by)))
const linkedDocs=computed(()=>{const ids=[props.doc?.supersedes,props.doc?.superseded_by].filter(Boolean) as string[];return ids.map(id=>props.docs.find(d=>d.id===id)).filter(Boolean) as PolicyDocument[]})
const otherOptions=computed(()=>props.docs.filter(d=>d.id!==docId.value))
const otherDoc=computed(()=>props.docs.find(d=>d.id===otherId.value)||null)

function statusLabel(status?:string){return ({active:'现行',superseded:'已被替代',repealed:'已废止'} as Record<string,string>)[status||'']||status||'未知'}
function docTitle(id?:string|null){if(!id)return '';return props.docs.find(d=>d.id===id)?.title||id}

async function fetchUnits(id:string):Promise<Unit[]>{
 if(unitCache.has(id))return unitCache.get(id)!
 const response=await fetch(`/api/documents/${encodeURIComponent(id)}/units`)
 if(!response.ok){let detail=`读取失败（${response.status}）`;try{const body=await response.json();if(typeof body.detail==='string')detail=body.detail}catch{}throw new Error(detail)}
 const payload=await response.json()
 const list:Unit[]=Array.isArray(payload.units)?payload.units:[]
 unitCache.set(id,list)
 return list
}
async function loadUnits(){
 if(!docId.value||unitsLoading.value)return
 unitsLoading.value=true;unitsError.value=''
 try{units.value=await fetchUnits(docId.value)}catch(e:any){unitsError.value=e?.message||'读取条款失败'}finally{unitsLoading.value=false}
}
async function loadOther(){
 if(!otherId.value||otherLoading.value)return
 otherLoading.value=true;otherError.value=''
 try{otherUnits.value=await fetchUnits(otherId.value)}catch(e:any){otherError.value=e?.message||'读取对照文档失败'}finally{otherLoading.value=false}
}
function normalize(text:string){return String(text||'').replace(/\s+/g,' ').trim()}
function keyOf(unit:Unit){if(unit.article)return 'a:'+normalize(unit.article);if(unit.chapter)return 'c:'+normalize(unit.chapter);return 'id:'+unit.id}
function labelOf(unit:Unit){return unit.article||unit.chapter||(unit.kind==='table_row'?'表格行':'正文')}
const rows=computed<Row[]>(()=>{
 if(!units.value.length||!otherUnits.value.length)return []
 const own=structured(units.value),other=structured(otherUnits.value)
 const mapOther=new Map(other.map(u=>[keyOf(u),u]))
 const mapOwn=new Set(own.map(u=>keyOf(u)))
 const list:Row[]=[]
 for(const unit of own){
  const key=keyOf(unit),match=mapOther.get(key)
  list.push({label:labelOf(unit),left:unit,right:match,status:!match?'onlyLeft':normalize(unit.text)===normalize(match.text)?'same':'changed'})
 }
 for(const unit of other){
  const key=keyOf(unit)
  if(!mapOwn.has(key))list.push({label:labelOf(unit),right:unit,status:'onlyRight'})
 }
 return list
})
const visibleRows=computed(()=>onlyDiff.value?rows.value.filter(row=>row.status!=='same'):rows.value)
const diffCount=computed(()=>rows.value.filter(row=>row.status!=='same').length)
function splitDiff(left:string,right:string):Diff{
 const a=String(left||''),b=String(right||'')
 let start=0
 while(start<a.length&&start<b.length&&a[start]===b[start])start++
 let endA=a.length,endB=b.length
 while(endA>start&&endB>start&&a[endA-1]===b[endB-1]){endA--;endB--}
 return {prefix:a.slice(0,start),left:a.slice(start,endA),right:b.slice(start,endB),suffix:a.slice(endA)}
}
function rowDiff(row:Row):Diff{return splitDiff(row.left?.text||'',row.right?.text||'')}
function pdfPageFor(unit?:Unit){return unit?.pages?.[0]||1}

function structured(list:Unit[]){const rows=list.filter(u=>u.article);return rows.length>=3?rows:list}
watch(()=>[tab.value,docId.value],()=>{
 if(tab.value==='text')loadUnits()
 if(tab.value==='compare'){
  loadUnits()
  if(!otherId.value)otherId.value=linkedDocs.value[0]?.id||''
 }
}, {immediate:true})
watch(otherId,()=>{if(tab.value==='compare'){otherUnits.value=[];loadOther()}})
watch(()=>props.source,()=>{pdfMode.value=false;tab.value='text'})

function markdown():string{
 const title=props.doc?.title||props.source?.title||props.source?.file||'文档'
 const lines:string[]=[]
 if(props.question){lines.push(`# ${props.question}`,'','## 回答','',props.answer||'','')}
 lines.push(`## 来源`,'')
 if(props.sources?.length){
  for(const s of props.sources)lines.push(`- [${s.id}] ${s.title||s.file}${s.article?' · '+s.article:''}（第${(s.pages||[1]).join('、')}页）`)
 }else if(units.value.length){
  for(const u of units.value)lines.push(`- ${labelOf(u)}：${u.text.slice(0,80)}${u.text.length>80?'…':''}`)
 }else lines.push(`- ${title}`)
 lines.push('',`> 由 PolicyRAG 生成 · ${new Date().toLocaleString('zh-CN')}`,'')
 return lines.join('\n')
}
function evidenceJson():string{
 const payload={question:props.question||'',answer:props.answer||'',sources:(props.sources||[]).map(s=>({id:s.id,doc_id:s.doc_id,title:s.title||s.file,article:s.article,pages:s.pages,text:s.text})),units:units.value.map(u=>({label:labelOf(u),text:u.text}))}
 return JSON.stringify(payload,null,2)
}
async function copy(text:string,label:string){
 try{await navigator.clipboard.writeText(text)}catch{const area=document.createElement('textarea');area.value=text;document.body.appendChild(area);area.select();document.execCommand('copy');area.remove()}
 copied.value=label;setTimeout(()=>{copied.value=''},2000)
}
function download(name:string,content:string,type:string){
 const blob=new Blob([content],{type}),url=URL.createObjectURL(blob),link=document.createElement('a')
 link.href=url;link.download=name;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)
}
function downloadMarkdown(){download(`policy-rag-${filterName(props.question||'文档')}.md`,markdown(),'text/markdown;charset=utf-8')}
function downloadJson(){download(`policy-rag-证据-${filterName(props.question||'文档')}.json`,evidenceJson(),'application/json;charset=utf-8')}
function filterName(text:string){return String(text||'export').replace(/[\\/:*?"<>|\s]+/g,'-').slice(0,40)||'export'}
</script>

<template>
 <header class="wb-head"><div class="wb-title"><strong>工作台</strong><span v-if="doc">{{doc.title||doc.name||doc.id}}</span></div><button class="icon-button" aria-label="关闭工作台" @click="emit('close')"><Icon name="close"/></button></header>
 <nav class="wb-tabs" aria-label="工作台视图">
  <button :class="{active:tab==='text'}" @click="tab='text'">原文</button>
  <button :class="{active:tab==='compare'}" @click="tab='compare'">对比<span v-if="comparable" class="tab-dot" aria-label="有可对照版本"></span></button>
  <button :class="{active:tab==='export'}" @click="tab='export'">导出</button>
 </nav>
 <section class="wb-body">
  <template v-if="tab==='text'">
   <dl v-if="doc" class="wb-meta">
    <dt>状态</dt><dd><span class="badge" :class="{success:doc.status==='active',processing:doc.status==='superseded'}">{{statusLabel(doc.status)}}</span></dd>
    <dt>归口部门</dt><dd>{{doc.department||'—'}}</dd>
    <dt>生效</dt><dd>{{doc.effective_from||'—'}}{{doc.effective_to?' 至 '+doc.effective_to:''}}</dd>
    <template v-if="doc.doc_no||doc.version_label"><dt>文号/版本</dt><dd>{{[doc.doc_no,doc.version_label].filter(Boolean).join(' · ')}}</dd></template>
    <template v-if="doc.supersedes||doc.superseded_by"><dt>版本链</dt><dd><span v-if="doc.superseded_by">已被替代 → {{docTitle(doc.superseded_by)}}</span><span v-if="doc.supersedes"> 本版替代 → {{docTitle(doc.supersedes)}}</span></dd></template>
   </dl>
   <div v-if="doc" class="wb-doc-actions"><button v-if="doc.status!=='repealed'" class="quiet-button" :disabled="busy" @click="emit('toggleStatus',doc)">停用该文档</button><button v-else class="quiet-button" :disabled="busy" @click="emit('toggleStatus',doc)">恢复文档</button></div>
   <div class="wb-view-toggle">
    <div class="source-tabs"><button :class="{selected:!pdfMode}" @click="pdfMode=false">条款文本</button><button v-if="hasPdf" :class="{selected:pdfMode}" @click="pdfMode=true">PDF 原文</button></div>
    <a v-if="hasPdf" class="wb-open-pdf" :href="pdfUrl.split('#')[0]" target="_blank" rel="noopener">新窗口打开</a>
   </div>
   <PdfPreview v-if="pdfMode&&hasPdf" :url="pdfUrl" :pages="pages" @page="emit('pdfPage',$event)"/>
   <div v-else-if="source?.text" class="wb-text"><div class="source-page-label">第{{(source.pages||[1]).join('、')}}页</div><pre>{{source.text}}</pre></div>
   <div v-else class="wb-units">
    <div v-if="unitsLoading" class="panel-empty">正在读取条款…</div>
    <div v-else-if="unitsError" class="inline-error">{{unitsError}} <button class="quiet-button" @click="loadUnits">重试</button></div>
    <template v-else-if="units.length">
     <article v-for="u in units" :key="u.id" class="wb-unit"><h3>{{labelOf(u)}}<span v-if="u.pages?.length"> · 第{{u.pages.join('、')}}页</span></h3><p>{{u.text}}</p></article>
    </template>
    <p v-else class="panel-empty">该文档没有条款文本，可查看 PDF 原文；从回答的“参考来源”打开时这里显示引用的证据原文。</p>
   </div>
  </template>

  <template v-else-if="tab==='compare'">
   <div class="wb-compare-bar">
    <label>对照版本<select v-model="otherId" aria-label="选择对照文档"><option value="">选择文档</option><option v-for="d in otherOptions" :key="d.id" :value="d.id">{{d.title||d.name||d.id}}{{d.version_label?' · '+d.version_label:''}}</option></select></label>
    <label class="wb-switch"><input v-model="onlyDiff" type="checkbox">只看差异<span v-if="diffCount" class="wb-diff-count">{{diffCount}}</span></label>
   </div>
   <p v-if="!comparable&&!otherId" class="wb-hint">该文档没有登记新旧版本。可从上方任选一份文档对照条款差异。</p>
   <div v-if="unitsLoading||otherLoading" class="panel-empty">正在对齐条款…</div>
   <div v-else-if="unitsError||otherError" class="inline-error">{{unitsError||otherError}} <button class="quiet-button" @click="unitsError?loadUnits():loadOther()">重试</button></div>
   <div v-else-if="!otherId" class="panel-empty">选择一份文档开始对比。</div>
   <p v-else-if="!visibleRows.length" class="panel-empty">{{rows.length?'两版条款内容一致':'没有可比对的条款'}}</p>
   <div v-else class="wb-rows" role="list">
    <div v-for="(row,i) in visibleRows" :key="i" class="wb-row" :class="row.status" role="listitem">
     <div class="wb-row-head"><span class="wb-row-label">{{row.label}}</span><span class="wb-badge" :class="row.status">{{row.status==='changed'?'修改':row.status==='onlyLeft'?'仅本版':row.status==='onlyRight'?'仅对照版':'一致'}}</span><button v-if="pdfPageFor(row.left||row.right)" class="quiet-button wb-jump" :title="'跳到第'+pdfPageFor(row.left||row.right)+'页'" @click="emit('pdfPage',pdfPageFor(row.left||row.right));tab='text';pdfMode=true">第{{pdfPageFor(row.left||row.right)}}页</button></div>
     <div v-if="row.status==='changed'" class="wb-diff"><div class="wb-diff-col"><span class="wb-diff-tag">本版</span><p>{{rowDiff(row).prefix}}<del>{{rowDiff(row).left}}</del>{{rowDiff(row).suffix}}</p></div><div class="wb-diff-col"><span class="wb-diff-tag other">对照版</span><p>{{rowDiff(row).prefix}}<ins>{{rowDiff(row).right}}</ins>{{rowDiff(row).suffix}}</p></div></div>
     <div v-else class="wb-diff single"><div class="wb-diff-col"><p>{{(row.left||row.right)?.text}}</p></div></div>
    </div>
   </div>
  </template>

  <template v-else>
   <p class="wb-hint">把当前回答与引用证据一起带走，便于归档或转发。</p>
   <div class="wb-export">
    <button class="wb-export-item" @click="copy(markdown(),'已复制回答 Markdown')"><Icon name="copy"/><span><strong>复制为 Markdown</strong><small>问题、回答与来源清单</small></span></button>
    <button class="wb-export-item" @click="downloadMarkdown"><Icon name="download"/><span><strong>下载 Markdown 文件</strong><small>policy-rag-*.md</small></span></button>
    <button class="wb-export-item" @click="copy(evidenceJson(),'已复制证据 JSON')"><Icon name="copy"/><span><strong>复制证据清单 JSON</strong><small>引用编号、条款与原文</small></span></button>
    <button class="wb-export-item" @click="downloadJson"><Icon name="download"/><span><strong>下载证据清单 JSON</strong><small>便于二次处理或归档</small></span></button>
    <a v-if="hasPdf" class="wb-export-item" :href="pdfUrl.split('#')[0]" download><Icon name="file"/><span><strong>下载 PDF 原文</strong><small>{{doc?.title||source?.title||docId}}</small></span></a>
   </div>
   <p v-if="copied" class="wb-copied" role="status">{{copied}}</p>
  </template>
 </section>
</template>

<style scoped>
.wb-head{height:66px;flex-shrink:0;display:flex;align-items:center;justify-content:space-between;padding:0 22px;border-bottom:1px solid var(--line)}
.wb-title{display:flex;flex-direction:column;gap:2px;min-width:0}
.wb-title strong{font-size:15px}
.wb-title span{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wb-tabs{display:flex;gap:6px;padding:10px 18px 0;border-bottom:1px solid var(--line);flex-shrink:0}
.wb-tabs button{padding:7px 10px 11px;font-size:14px;color:var(--muted);border-bottom:2px solid transparent;display:inline-flex;align-items:center;gap:6px}
.wb-tabs button.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-dot{width:5px;height:5px;border-radius:50%;background:var(--accent)}
.wb-body{overflow:auto;padding:18px 22px 26px;flex:1;min-height:0}
.wb-meta{display:grid;grid-template-columns:66px 1fr;gap:9px 12px;margin:0 0 14px;font-size:13px;line-height:1.6}
.wb-meta dt{color:var(--muted)}
.wb-meta dd{margin:0;overflow-wrap:anywhere}
.wb-doc-actions{display:flex;gap:8px;margin:0 0 14px}
.wb-view-toggle{display:flex;align-items:flex-end;gap:12px;border-bottom:1px solid var(--line);margin-bottom:12px}
.wb-open-pdf{margin-left:auto;font-size:13px;text-decoration:none;padding-bottom:11px}
.source-tabs{display:flex;gap:18px}
.source-tabs button{padding:0 0 11px;font-size:14px;border-bottom:2px solid transparent}
.source-tabs button.selected{color:var(--accent);border-bottom-color:var(--accent)}
.wb-text pre{font:14px/1.85 inherit;white-space:pre-wrap;overflow-wrap:anywhere;margin:10px 0}
.wb-units{display:flex;flex-direction:column;gap:14px}
.wb-unit h3{margin:0 0 4px;font-size:13px;color:#5c6c62;font-weight:600}
.wb-unit h3 span{font-weight:400;color:var(--muted)}
.wb-unit p{margin:0;font-size:14px;line-height:1.8;white-space:pre-wrap;overflow-wrap:anywhere}
.wb-compare-bar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:12px}
.wb-compare-bar label{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:7px}
.wb-compare-bar select{height:30px;border:1px solid var(--line);border-radius:7px;background:#fff;font-size:13px;padding:0 7px;max-width:260px}
.wb-switch{gap:5px!important}
.wb-diff-count{background:#eaf1eb;color:var(--accent);border-radius:9px;padding:1px 7px;font-size:12px}
.wb-hint{font-size:13px;color:var(--muted);line-height:1.7;margin:0 0 12px}
.wb-rows{display:flex;flex-direction:column;gap:10px}
.wb-row{border:1px solid var(--line);border-radius:9px;padding:10px 12px;background:#fff}
.wb-row.changed{border-color:#d8e4d3}
.wb-row.onlyLeft,.wb-row.onlyRight{border-color:#ebe0d2}
.wb-row-head{display:flex;align-items:center;gap:8px;margin-bottom:5px}
.wb-row-label{font-size:13px;font-weight:600;flex:1;overflow-wrap:anywhere}
.wb-badge{font-size:11px;padding:2px 7px;border-radius:8px;background:var(--soft);color:var(--muted)}
.wb-badge.changed{background:#eef4e8;color:#4c7a45}
.wb-badge.onlyLeft,.wb-badge.onlyRight{background:#f6efe3;color:#8a6432}
.wb-badge.same{background:#f2f3f1}
.wb-jump{font-size:12px;padding:2px 7px!important;color:var(--muted)}
.wb-diff{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.wb-diff.single{grid-template-columns:1fr}
.wb-diff-col{min-width:0}
.wb-diff-tag{font-size:11px;color:var(--muted);display:inline-block;margin-bottom:2px}
.wb-diff-col p{margin:0;font-size:13px;line-height:1.75;white-space:pre-wrap;overflow-wrap:anywhere}
.wb-diff del{background:#fbe9e6;color:#9c4a3d;text-decoration:none;border-radius:2px}
.wb-diff ins{background:#e8f3e6;color:#3f6f3a;text-decoration:none;border-radius:2px}
.wb-export{display:flex;flex-direction:column;gap:8px}
.wb-export-item{display:flex;align-items:center;gap:12px;text-align:left;padding:11px 12px;border:1px solid var(--line);border-radius:9px;background:#fff;font-size:14px}
.wb-export-item:hover{background:var(--soft)}
.wb-export-item svg{color:var(--accent);flex-shrink:0}
.wb-export-item span{display:flex;flex-direction:column;gap:2px;min-width:0}
.wb-export-item strong{font-weight:600}
.wb-export-item small{font-size:12px;color:var(--muted);overflow-wrap:anywhere}
a.wb-export-item{text-decoration:none;color:inherit}
.wb-copied{font-size:13px;color:var(--accent);margin:10px 0 0}
.panel-empty{padding:40px 10px;text-align:center;color:var(--muted);font-size:13px}
.inline-error{background:#fff3e7;border-radius:7px;padding:10px 13px;font-size:13px;color:#905922;margin:12px 0}
@media(max-width:1150px){.wb-diff{grid-template-columns:1fr}}
</style>
