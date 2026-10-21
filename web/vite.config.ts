import {defineConfig,loadEnv} from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig(({mode})=>{
  const env=loadEnv(mode,process.cwd(),'POLICYRAG_')
  const target=process.env.POLICYRAG_API_URL||env.POLICYRAG_API_URL||'http://127.0.0.1:7880'
  return {plugins:[vue()],server:{host:'127.0.0.1',proxy:{'/api':{target,changeOrigin:true}}},build:{sourcemap:false}}
})
