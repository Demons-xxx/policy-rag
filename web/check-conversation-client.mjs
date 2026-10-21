// Guard: keep the /api/ask NDJSON stream and drop the old conversation polling.
// (This file replaces the original conversation-client test after PolicyRAG trimming.)
import {readFileSync} from 'node:fs'
import assert from 'node:assert/strict'
const app=readFileSync(new URL('./src/App.vue',import.meta.url),'utf8')
for(const required of ["buffer.indexOf('\\n')","e.stage==='answer'","e.stage==='sources'","e.stage==='done'","e.stage==='error'","run_tag:'web'",'controller?.abort()']){
 assert.ok(app.includes(required),`App.vue 缺少流式问答处理：${required}`)
}
for(const forbidden of ['/api/conversation','X-Agent-Session','request_id','conversation_id','document.hidden?2500']){
 assert.ok(!app.includes(forbidden),`App.vue 仍包含旧轮询逻辑：${forbidden}`)
}
console.log('PASS: /api/ask 逐行 NDJSON、错误与中止处理保留；旧对话轮询已移除')
