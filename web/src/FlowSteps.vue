<script setup lang="ts">
import { reactive } from 'vue'
import Icon from './Icon.vue'

const props = defineProps<{ events: any[]; seconds?: number; firstTokenMs?: number }>()

const LABELS: Record<string, string> = {
  routing: '意图路由', query: '问题解析', vectors: '向量召回', keywords: '关键词召回',
  evidence: '证据组装', prompt: '提示词', generating: '生成回答', revision: '后置校验',
  sources: '引用整理', done: '完成', error: '错误', waiting: '等待',
}
const expanded = reactive(new Set<number>())

function label(stage: string): string { return LABELS[stage] || stage }
function evidenceEvent(): any {
  for (let i = props.events.length - 1; i >= 0; i -= 1) if (props.events[i]?.stage === 'evidence') return props.events[i]
  return null
}
function ms(index: number): number | null {
  if (index <= 0) return null
  const prev = props.events[index - 1]?.at
  const cur = props.events[index]?.at
  if (typeof prev !== 'number' || typeof cur !== 'number' || cur < prev) return null
  return Math.round(cur - prev)
}
function fmtMs(value: number | null): string {
  if (value == null) return ''
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${value}ms`
}
function fmtScore(value: any): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(Math.round(value * 1000) / 1000) : '—'
}
function scoreText(scores: any): string {
  if (!scores || typeof scores !== 'object') return '—'
  const names: Record<string, string> = { dense: '向量', bm25: 'BM25', rrf: 'RRF', rerank: '重排' }
  return Object.entries(scores).map(([key, value]) => `${names[key] || key} ${fmtScore(value)}`).join(' · ')
}
function promptPreview(e: any): string {
  const messages = e.messages || []
  const system = String(messages[0]?.content || '').slice(0, 220)
  const user = String(messages[messages.length - 1]?.content || '')
  return `系统要求：\n${system}\n\n用户输入（节选）：\n${user.slice(0, 700)}`
}
function promptFull(e: any): string {
  const roleName: Record<string, string> = { system: '系统', user: '用户', assistant: '助手' }
  return (e.messages || []).map((m: any) => `${roleName[m.role] || m.role}：\n${m.content}`).join('\n\n')
}
function promptChars(e: any): number {
  return (e.messages || []).reduce((sum: number, m: any) => sum + String(m?.content || '').length, 0)
}
function raw(e: any): string {
  const copy: any = { ...e }
  delete copy.at
  const text = JSON.stringify(copy, null, 1)
  return text.length > 2600 ? `${text.slice(0, 2600)}\n…（原始事件过长，已截断）` : text
}
function revisionText(e: any): string {
  const parts: string[] = []
  if (Array.isArray(e.removed_invalid) && e.removed_invalid.length) parts.push(`删除越界引用 [证据${e.removed_invalid.join('][证据')}]`)
  if (e.removed_for_refusal) parts.push(`拒答与引用矛盾，清除 ${e.removed_for_refusal} 处引用`)
  if (e.dropped_refusal) parts.push('删除与证据矛盾的拒答句')
  return parts.join('；') || '回答经规则修正'
}
function summary(): string {
  const evidence = evidenceEvent()
  const count = evidence?.count ?? evidence?.titles?.length ?? 0
  const total = props.seconds != null ? `${props.seconds}s` : ''
  const hit = evidence?.cached ? ' · 缓存命中' : ''
  return [count ? `${count} 条证据` : '', total ? `总 ${total}` : ''].filter(Boolean).join(' · ') + hit
}
</script>

<template>
  <details class="flow-block" open>
    <summary>
      <span class="flow-heading">检索流程</span>
      <span class="flow-total">{{ summary() }}</span>
      <Icon name="chevron" />
    </summary>
    <ol class="flow-steps">
      <li v-for="(e, i) in events" :key="i" class="flow-step" :class="{ bad: e.stage === 'error' }">
        <span class="step-dot" />
        <div class="flow-main">
          <div class="flow-title">
            <strong>{{ label(e.stage) }}</strong>
            <span v-if="ms(i) != null" class="flow-ms">{{ fmtMs(ms(i)) }}</span>
          </div>

          <template v-if="e.stage === 'routing'">
            <div class="flow-row">
              <span class="chip" :class="{ ok: e.mode !== 'chat' }">{{ e.mode === 'chat' ? '闲聊路径' : '制度检索' }}</span>
              <span v-if="e.reason" class="flow-note">{{ e.reason }}</span>
            </div>
            <div v-if="e.search_query" class="flow-line">检索问句：{{ e.search_query }}</div>
          </template>

          <template v-else-if="e.stage === 'query'">
            <div class="flow-line">原问题：{{ e.question }}</div>
            <div v-if="e.rewritten" class="flow-line">改写后：{{ e.rewritten }}</div>
            <div v-if="e.sub_queries && e.sub_queries.length" class="flow-line">
              子问题：<span v-for="(q, j) in e.sub_queries" :key="j" class="chip">{{ q }}</span>
            </div>
            <div class="flow-row">
              <span v-if="e.top_k" class="chip">top {{ e.top_k }}</span>
              <span v-if="e.filters?.department" class="chip">部门 {{ e.filters.department }}</span>
              <span v-if="e.filters?.as_of" class="chip">时点 {{ e.filters.as_of }}</span>
              <span v-if="e.doc_no" class="chip">文号 {{ e.doc_no }}</span>
              <span v-if="e.article" class="chip">条款 {{ e.article }}</span>
              <span v-if="e.release_id" class="chip">发布 {{ e.release_id }}</span>
              <span v-if="e.is_followup" class="chip">追问补全</span>
            </div>
          </template>

          <template v-else-if="e.stage === 'vectors' || e.stage === 'keywords'">
            <div class="flow-row">
              <span class="chip">{{ e.passage_count ?? 0 }} 个候选片段</span>
              <span class="chip">最高分 {{ fmtScore(e.max_score) }}</span>
              <span class="chip">{{ e.stage === 'vectors' ? 'BGE 向量' : 'BM25 关键词' }}</span>
            </div>
            <div v-if="e.titles?.length" class="flow-hits">
              <div v-for="(t, j) in e.titles.slice(0, 5)" :key="j" class="flow-hit">
                <span class="hit-score">{{ fmtScore((e.scores || [])[j]) }}</span>
                <span class="hit-text">{{ t.title || t }}</span>
              </div>
            </div>
          </template>

          <template v-else-if="e.stage === 'evidence'">
            <div class="flow-row">
              <span class="chip" :class="{ ok: (e.count || 0) > 0 }">{{ e.count ?? e.titles?.length ?? 0 }} 条证据</span>
              <span v-if="e.tokens != null" class="chip">证据 {{ e.tokens }} / 预算 {{ e.budget || '—' }} tokens</span>
              <span v-if="e.seconds != null && !e.cached" class="chip">检索 {{ e.seconds }}s</span>
              <span v-if="e.cached" class="chip ok">缓存命中</span>
            </div>
            <div v-if="e.cached && !events.some(x => x.stage === 'vectors')" class="flow-line">命中检索缓存：向量/关键词召回已跳过，直接复用上次证据</div>
            <ul v-if="e.notes?.length" class="flow-notes">
              <li v-for="(n, j) in e.notes" :key="j">{{ n }}</li>
            </ul>
            <table v-if="e.items?.length" class="flow-table">
              <thead><tr><th>#</th><th>证据</th><th>通道</th><th>分数</th></tr></thead>
              <tbody>
                <tr v-for="item in e.items" :key="item.rank">
                  <td>{{ item.rank }}</td>
                  <td>{{ item.label }}<span v-if="item.merged" class="chip">合并 {{ item.merged }}</span></td>
                  <td><span v-for="(c, k) in item.channels" :key="k" class="chip ch">{{ c }}</span></td>
                  <td class="flow-scores">{{ scoreText(item.scores) }}</td>
                </tr>
              </tbody>
            </table>
          </template>

          <template v-else-if="e.stage === 'prompt'">
            <div class="flow-row">
              <span class="chip">{{ (e.messages || []).length }} 条消息</span>
              <span class="chip">{{ promptChars(e) }} 字符</span>
              <button class="flow-link" type="button" @click="expanded.has(i) ? expanded.delete(i) : expanded.add(i)">
                {{ expanded.has(i) ? '收起全文' : '展开全文' }}
              </button>
            </div>
            <pre class="flow-prompt">{{ expanded.has(i) ? promptFull(e) : promptPreview(e) }}</pre>
          </template>

          <template v-else-if="e.stage === 'generating'">
            <div class="flow-row">
              <span class="chip">首字延迟 {{ firstTokenMs != null ? fmtMs(firstTokenMs) : '—' }}</span>
              <span class="chip">按帧平滑输出</span>
            </div>
          </template>

          <template v-else-if="e.stage === 'revision'">
            <div class="flow-line">{{ revisionText(e) }}</div>
          </template>

          <template v-else-if="e.stage === 'sources'">
            <div class="flow-row"><span class="chip">{{ e.count ?? (e.items || []).length }} 条引用</span></div>
          </template>

          <template v-else-if="e.stage === 'done'">
            <div class="flow-row">
              <span class="chip">总耗时 {{ e.seconds }}s</span>
              <span v-if="e.cached" class="chip ok">检索缓存</span>
              <span class="chip" :class="{ bad: e.output_truncated }">{{ e.output_truncated ? '输出被截断' : '输出完整' }}</span>
            </div>
          </template>

          <template v-else-if="e.stage === 'error'">
            <div class="flow-line bad">{{ e.message }}</div>
          </template>

          <details class="flow-raw">
            <summary>原始事件</summary>
            <pre>{{ raw(e) }}</pre>
          </details>
        </div>
      </li>
    </ol>
  </details>
</template>

<style scoped>
.flow-block{margin:0 0 22px;border:1px solid var(--line,#e7eae6);border-radius:10px;font-size:13px;background:#fcfdfb}
.flow-block>summary{list-style:none;display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;color:#5e7166}
.flow-block>summary::-webkit-details-marker{display:none}
.flow-block[open]>summary{border-bottom:1px solid var(--line,#e7eae6)}
.flow-block>summary svg{width:14px;height:14px;margin-left:auto;transition:transform .18s ease-out}
.flow-block[open]>summary svg{transform:rotate(90deg)}
.flow-heading{font-weight:600;color:#3d4f45}
.flow-total{margin-left:auto;color:var(--muted,#75807b);font-size:12px;font-variant-numeric:tabular-nums}
.flow-steps{list-style:none;margin:0;padding:4px 14px 12px}
.flow-step{position:relative;display:flex;gap:10px;padding:9px 0 9px 2px;border-bottom:1px dashed #eef1ec}
.flow-step:last-child{border-bottom:0}
.step-dot{width:6px;height:6px;border-radius:50%;background:#9bb6a4;margin-top:7px;flex-shrink:0}
.flow-step.bad .step-dot{background:#c9735f}
.flow-main{min-width:0;flex:1}
.flow-title{display:flex;align-items:baseline;gap:8px;line-height:1.5}
.flow-title strong{font-weight:600;color:#31443a}
.flow-ms{margin-left:auto;color:#93a09a;font-size:12px;font-variant-numeric:tabular-nums}
.flow-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:5px;align-items:center}
.chip{display:inline-flex;align-items:center;gap:4px;border-radius:5px;background:#eef2ea;color:#4f6155;padding:2px 7px;font-size:12px;line-height:1.6;margin-right:4px}
.chip.ok{background:#e3efe5;color:#2f6b47}
.chip.bad{background:#fbeee9;color:#a4553f}
.chip.ch{background:#f2f4f0;color:#6c7a70;font-variant-numeric:tabular-nums}
.flow-line{margin-top:5px;color:#5f6f65;line-height:1.65;overflow-wrap:anywhere}
.flow-line.bad{color:#a4553f}
.flow-note{color:var(--muted,#75807b);font-size:12px}
.flow-hits{margin-top:6px;display:flex;flex-direction:column;gap:3px}
.flow-hit{display:flex;gap:8px;align-items:baseline;min-width:0;color:#5d6d63}
.hit-score{flex-shrink:0;width:52px;text-align:right;font-variant-numeric:tabular-nums;color:#8b988f;font-size:12px}
.hit-text{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.flow-notes{margin:6px 0 0;padding-left:18px;color:#7b6a4a;line-height:1.7}
.flow-table{width:100%;border-collapse:collapse;margin-top:8px;font-size:12px}
.flow-table th{text-align:left;color:#8b988f;font-weight:500;padding:4px 8px 4px 0;border-bottom:1px solid #eef1ec}
.flow-table td{padding:5px 8px 5px 0;border-bottom:1px dashed #f1f3ee;vertical-align:top;color:#4d5e54}
.flow-scores{color:#7c8a80;font-variant-numeric:tabular-nums;white-space:nowrap}
.flow-link{border:0;background:none;color:var(--accent,#287b69);font-size:12px;padding:0 2px;cursor:pointer}
.flow-prompt{margin:7px 0 0;padding:9px 11px;background:#f7f9f5;border-radius:7px;color:#5f6f65;font:12px/1.7 inherit;white-space:pre-wrap;max-height:180px;overflow:auto}
.flow-raw{margin-top:5px}
.flow-raw>summary{list-style:none;cursor:pointer;color:#98a39c;font-size:11px}
.flow-raw>summary::-webkit-details-marker{display:none}
.flow-raw>summary:after{content:' ›'}
.flow-raw>pre{margin:6px 0 0;padding:9px 11px;background:#f4f6f2;border-radius:7px;font:11px/1.6 ui-monospace,monospace;color:#5d6d63;white-space:pre-wrap;max-height:220px;overflow:auto}
@media(prefers-reduced-motion:reduce){.flow-block>summary svg{transition:none}}
</style>
