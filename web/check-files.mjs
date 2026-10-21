import {readFileSync} from 'node:fs'
import assert from 'node:assert/strict'
import ts from 'typescript'
const source=readFileSync(new URL('./src/fileSelection.ts',import.meta.url),'utf8')
const code=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText
const {selectFiles}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))
const mb=1024*1024,f=(name,size=mb,lastModified=1)=>({name,size,lastModified})
const original=[f('existing.pdf')]
assert.equal(selectFiles([], [f('a.pdf'),f('b.docx'),f('c.md'),f('d.txt')]).files.length,4)
assert.equal(selectFiles([],[f('UPPER.PDF')]).files.length,1)
assert.equal(selectFiles(original,[f('bad.exe')]).files,original)
assert.match(selectFiles(original,[f('bad.exe')]).error,/PDF、DOCX、MD或TXT/)
assert.match(selectFiles(original,[f('empty.pdf',0)]).error,/为空/)
assert.match(selectFiles(original,[f('big.pdf',201*mb)]).error,/200MB/)
assert.match(selectFiles(original,Array.from({length:21},(_,i)=>f(`${i}.pdf`))).error,/20份/)
assert.match(selectFiles([],[f('1.pdf',200*mb),f('2.pdf',200*mb),f('3.pdf',101*mb)]).error,/500MB/)
assert.equal(selectFiles(original,[original[0]]).files.length,1)
assert.match(selectFiles(original,[f('existing.pdf',mb,2)]).error,/同名/)
assert.equal(selectFiles(original,[f('new.docx')]).files.length,2)
console.log('PASS: PDF/DOCX/MD/TXT 类型、空文件/超限、数量与总大小、同名处理')
