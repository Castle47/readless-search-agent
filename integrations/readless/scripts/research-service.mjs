import {researchMode} from './research-mode.mjs';
import {spawn} from 'node:child_process';
import {access,mkdir} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {readResearchSecrets} from './research-http.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const command=process.argv[2]||'status';
if(['start','restart','provision','index','index-folder','index-site-only'].includes(command)&&researchMode()!=='local')throw new Error('当前为部署展示模式，本机研究服务未启用。');
const dockerDir='/Applications/Docker.app/Contents/Resources/bin';
let executable='docker';
try{await access(path.join(dockerDir,'docker'));executable=path.join(dockerDir,'docker');}catch{}
const env={...process.env,PATH:dockerDir+path.delimiter+process.env.PATH};
const compose=['compose','-f',path.join(root,'research-agent/compose.yaml')];
async function run(args){
 await new Promise((resolve,reject)=>{const child=spawn(executable,[...compose,...args],{cwd:root,env,stdio:'inherit'});child.on('error',reject);child.on('exit',code=>code===0?resolve():reject(new Error(`Docker operation failed (${code}).`)));});
}
try{
 if(['index','index-folder','index-site-only'].includes(command))await new Promise((resolve,reject)=>{const child=spawn(executable,['info','--format','{{.ServerVersion}}'],{cwd:root,env,stdio:'ignore',timeout:10000,killSignal:'SIGKILL'});child.on('error',reject);child.on('exit',code=>code===0?resolve():reject(Error('Docker 研究服务未正常响应，请先恢复 Docker Desktop。')));});
 if(['start','restart'].includes(command)){
  const secrets=await readResearchSecrets(root);
  if(!secrets.openrouter_api_key)throw new Error('请先在 ReadLess 研究助手的“研究设置”保存 OpenRouter API key。');
  await mkdir(path.join(root,'.readless/research-folders'),{recursive:true,mode:0o700});
  await mkdir(path.join(root,'.readless/research-site'),{recursive:true,mode:0o700});
  await run(['up','-d','--build']);
  console.log('研究容器已启动。首次使用请运行 npm run research:provision，准备长期记忆。');
 }else if(command==='index-site-only'){
  await run(['exec','-T','api','research-agent','index-site']);
 }else if(command==='index-folder'){
  const id=process.argv[3];if(!/^[a-f0-9-]{36}$/.test(id||''))throw Error('无效的资料目录。');
  await run(['exec','-T','api','research-agent','index-folder',id]);
 }else if(command==='index'){
  const secrets=await readResearchSecrets(root);
  if(secrets.research_account_id){
   await new Promise((resolve,reject)=>{
    const child=spawn(process.execPath,[path.join(root,'research-agent/scripts/sync_readless_blogs.mjs'),secrets.research_account_id],{cwd:root,env,stdio:'inherit'});
    child.on('error',reject);child.on('exit',code=>code===0?resolve():reject(new Error('原创博客同步失败。')));
   });
  }
  await new Promise((resolve,reject)=>{const child=spawn(process.execPath,[path.join(root,'research-agent/scripts/sync_readless_site.mjs'),secrets.research_account_id||''],{cwd:root,env,stdio:'inherit'});child.on('error',reject);child.on('exit',code=>code===0?resolve():reject(Error('网站资料同步失败。')));});
  await run(['exec','-T','api','research-agent','index-blogs']);
  await run(['exec','-T','api','research-agent','index-site']);
 }else if(command==='provision'){
  await run(['exec','-T','embeddings','ollama','pull','nomic-embed-text']);
  await run(['exec','-T','api','research-agent','provision-memory']);
 }else if(command==='stop')await run(['stop']);
 else if(command==='status')await run(['ps']);
 else if(command==='logs')await run(['logs','--tail','80','api','worker','memory-worker']);
 else throw new Error('Usage: research-service.mjs start|restart|stop|status|provision|index|logs');
}catch(error){console.error(error.message);process.exitCode=1;}
