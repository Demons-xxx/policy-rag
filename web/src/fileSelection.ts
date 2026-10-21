export type UploadFile={name:string;size:number;lastModified:number}
const allowed=['.pdf','.docx','.md','.txt']
export function selectFiles<T extends UploadFile>(existing:T[],incoming:T[]):{files:T[];error?:string}{
 if(incoming.some(f=>!allowed.some(ext=>f.name.toLowerCase().endsWith(ext))))return {files:existing,error:'请选择PDF、DOCX、MD或TXT文件'}
 if(incoming.some(f=>f.size===0))return {files:existing,error:'文件为空，请重新选择'}
 if(incoming.some(f=>f.size>200*1024*1024))return {files:existing,error:'每份文件最大200MB'}
 const merged=[...existing]
 for(const f of incoming){
  const same=merged.find(v=>v.name===f.name)
  if(same){if(same.size!==f.size||same.lastModified!==f.lastModified)return {files:existing,error:'同名的不同文件请分批上传'};continue}
  merged.push(f)
 }
 if(merged.length>20)return {files:existing,error:'每批最多20份文件，请分批上传'}
 if(merged.reduce((n,f)=>n+f.size,0)>500*1024*1024)return {files:existing,error:'每批文件总大小不能超过500MB'}
 return {files:merged}
}
