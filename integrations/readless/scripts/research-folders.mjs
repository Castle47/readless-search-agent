import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {spawn} from 'node:child_process';
const failure=(message,status=400)=>Object.assign(Error(message),{status});
export function createResearchFolders(root,{indexFolder}={}){
 const base=path.join(root,'.readless/research-folders'),config=path.join(root,'.readless/research-config/folders.json');
 let busy=false;
 async function read(){try{return JSON.parse(await fs.readFile(config,'utf8'));}catch(e){if(e.code!=='ENOENT')throw e;return {folders:[]};}}
 async function atomic(file,data){await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});const tmp=file+'.'+randomUUID()+'.tmp';await fs.writeFile(tmp,JSON.stringify(data,null,2),{mode:0o600});await fs.rename(tmp,file);}
 async function save(data){await atomic(config,data);await atomic(path.join(base,'manifest.json'),{folders:data.folders.map(({id,name,status})=>({id,name,enabled:status==='ready'}))});}
 async function snapshot(folder){
  const source=await fs.realpath(folder.path),target=path.join(base,folder.id),temp=path.join(base,'.stage-'+randomUUID());
  await fs.mkdir(temp,{recursive:true,mode:0o700});let files=0,bytes=0;const skipped=[];
  async function visit(dir,relative=''){
   for(const entry of await fs.readdir(dir,{withFileTypes:true})){
    if(entry.name.startsWith('.')||['node_modules','__pycache__'].includes(entry.name))continue;
    const rel=path.join(relative,entry.name),file=path.join(dir,entry.name);
    if(entry.isSymbolicLink()){skipped.push({path:rel,reason:'符号链接不读取'});continue;}
    if(entry.isDirectory()){await visit(file,rel);continue;}
    if(!entry.isFile()||!['.md','.txt','.pdf'].includes(path.extname(entry.name).toLowerCase()))continue;
    const stat=await fs.stat(file);if(stat.size>50*1024*1024){skipped.push({path:rel,reason:'单文件超过 50 MiB'});continue;}
    if(++files>1000||(bytes+=stat.size)>256*1024*1024)throw failure('目录超过 1000 份文件或 256 MiB，请选择更小的子文件夹。');
    const actual=await fs.realpath(file);if(!actual.startsWith(source+path.sep))throw failure('文件路径已改变，请重试。');
    const dest=path.join(temp,rel);await fs.mkdir(path.dirname(dest),{recursive:true});await fs.copyFile(actual,dest);
   }
  }
  try{await visit(source);if(!files)throw failure('没有找到可用的 Markdown、TXT 或 PDF 文件。');await fs.rm(target,{recursive:true,force:true});await fs.rename(temp,target);return {files,bytes,skipped};}finally{await fs.rm(temp,{recursive:true,force:true});}
 }
 const runIndex=indexFolder||((id)=>new Promise((resolve,reject)=>{const child=spawn(process.execPath,[path.join(root,'scripts/research-service.mjs'),'index-folder',id],{cwd:root,stdio:['ignore','pipe','pipe']});let output='',errors='';child.stdout.on('data',b=>{output=(output+b).slice(-2000000);});child.stderr.on('data',b=>{errors=(errors+b).slice(-2000);});child.on('error',reject);child.on('exit',code=>{if(code!==0)return reject(Error('索引失败，请确认研究服务与本地向量模型已启动，然后重试。'));try{resolve(JSON.parse(output.trim().split('\n').at(-1)));}catch{reject(Error('索引结果无法读取，请重试。'));}});}));
 async function build(id){try{const data=await read(),folder=data.folders.find(f=>f.id===id);const copied=await snapshot(folder),report=await runIndex(id);folder.status=report.files>0?'ready':'failed';if(!report.files)folder.error='没有文件成功建立索引，请查看未纳入原因。';folder.report={...report,skipped:[...copied.skipped,...(report.skipped||[])]};folder.updatedAt=new Date().toISOString();await save(data);}catch(error){const data=await read(),folder=data.folders.find(f=>f.id===id);if(folder){folder.status='failed';folder.error=error.message;await save(data);}}finally{busy=false;}}
 return {async list(){const data=await read();return {...data,folders:data.folders.map(f=>f.status==='indexing'&&!busy?{...f,status:'failed',error:'上次索引被中断，请刷新重试。'}:f)};},async change(input){
  if(busy)throw failure('正在建立索引，请完成后再操作。',409);busy=true;
  try{const data=await read();let folder=data.folders.find(f=>f.id===input.id);
   if(input.action==='add'){
    if(typeof input.path!=='string'||!path.isAbsolute(input.path.trim()))throw failure('请输入本机文件夹的完整路径。');
    const source=await fs.realpath(input.path.trim()).catch(()=>{throw failure('文件夹不存在或无法访问。');});if(!(await fs.stat(source)).isDirectory())throw failure('请选择文件夹。');
    if(data.folders.some(f=>f.path===source))throw failure('此文件夹已添加，可点击刷新索引。');
    if(data.folders.length>=10)throw failure('最多添加 10 个文件夹。');
    folder={id:randomUUID(),path:source,name:path.basename(source),status:'indexing'};data.folders.push(folder);
   }else if(!folder)throw failure('找不到这个文件夹。',404);
   if(input.action==='remove'){data.folders=data.folders.filter(f=>f.id!==folder.id);await save(data);await fs.rm(path.join(base,folder.id),{recursive:true,force:true});busy=false;return data;}
   if(!['add','refresh'].includes(input.action))throw failure('不支持此操作。');
   folder.status='indexing';delete folder.error;await save(data);void build(folder.id);return data;
  }catch(error){busy=false;throw error;}
 }};
}
