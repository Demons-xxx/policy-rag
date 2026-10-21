// Guard: the industrial task-refresh / AgentTasks features must stay removed.
// (This file replaces the original task-polling test after PolicyRAG trimming.)
import {readFileSync,existsSync} from 'node:fs'
import assert from 'node:assert/strict'
const app=readFileSync(new URL('./src/App.vue',import.meta.url),'utf8')
const root=new URL('./src/',import.meta.url)
for(const forbidden of ['AgentTasks','followMessage','latestTask','/api/conversation','100题','参考答案','automatic_entry','resumeAutomatic']){
 assert.ok(!app.includes(forbidden),`App.vue 仍包含已移除功能：${forbidden}`)
}
assert.ok(!existsSync(new URL('AgentTasks.vue',root)),'AgentTasks.vue 仍存在')
assert.ok(!existsSync(new URL('conversationClient.ts',root)),'conversationClient.ts 仍存在')
for(const required of ["fetch('/api/ask'","stage==='sources'","has_pdf","PolicyRAG 政策制度问答",'accept=".pdf,.docx,.md,.txt"','输入你的问题，例如：实验室危化品怎么领用？']){
 assert.ok(app.includes(required),`App.vue 缺少必需内容：${required}`)
}
console.log('PASS: AgentTasks/100题/参考答案/对话轮询已移除，政策问答主流程与文件类型保留')
