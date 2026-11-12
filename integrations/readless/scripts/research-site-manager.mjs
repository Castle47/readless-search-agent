import fs from 'node:fs/promises';import path from 'node:path';import {spawn} from 'node:child_process';
export function createSiteManager(root,{execute}={}){
 const directory=path.join(root,'.readless/research-config'),file=path.join(directory,'site-index.json');let running=false;
 const run=execute||((args,timeout=0)=>new Promise((resolve,reject)=>{const child=spawn(process.execPath,args,{cwd:root,stdio:['ignore','pipe','pipe'],...(timeout?{timeout,killSignal:'SIGKILL'}:{})});let output='';child.stdout.on('data',b=>{output=(output+b).slice(-2000000);});child.stderr.resume();child.on('error',reject);child.on('exit',code=>{if(code!==0)return reject(Error('网站资料同步或索引失败，请检查网络、研究服务及本地向量模型。'));try{resolve(JSON.parse(output.trim().split('\n').at(-1)));}catch{reject(Error('网站索引结果无法读取。'));}});}));
 const save=async value=>{await fs.mkdir(directory,{recursive:true,mode:0o700});await fs.writeFile(file+'.tmp',JSON.stringify(value),{mode:0o600});await fs.rename(file+'.tmp',file);};
 async function status(){let value;try{value=JSON.parse(await fs.readFile(file,'utf8'));}catch(e){if(e.code!=='ENOENT')throw e;return {status:'not_indexed'};}if(value.status==='indexing'&&!running)return {...value,status:'failed',error:'上次索引中断，请刷新重试。'};return value;}
 async function index(accountId){if(running)return status();running=true;await save({status:'indexing',startedAt:new Date().toISOString()});void (async()=>{let sync;try{sync=await run([path.join(root,'research-agent/scripts/sync_readless_site.mjs'),accountId||'']);const report=await run([path.join(root,'scripts/research-service.mjs'),'index-site-only']);await save({status:'ready',sync,report,updatedAt:new Date().toISOString()});}catch(error){await save({status:'failed',sync,error:error.message});}finally{running=false;}})();return {status:'indexing'};}
 async function access(accountId){const destination=path.join(root,'.readless/research-site');try{await fs.access(path.join(destination,'manifest.json'));}catch{return;}
  try{await run([path.join(root,'research-agent/scripts/sync_readless_site.mjs'),accountId||'','--access-only'],8000);}catch{await fs.writeFile(path.join(destination,'access.json'),JSON.stringify({checkedAt:Date.now(),enabled:[]}));}
 }
 return {status,index,access};
}
