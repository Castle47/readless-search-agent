// Read-only site export. Original source files and cloud state are never modified.
import fs from 'node:fs/promises';import path from 'node:path';import {createHash,randomUUID} from 'node:crypto';import {gunzipSync} from 'node:zlib';
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const load=async file=>JSON.parse(await fs.readFile(file,'utf8'));
const dirs=async root=>(await fs.readdir(root,{withFileTypes:true}).catch(e=>{if(e.code==='ENOENT')return [];throw e;})).filter(f=>f.isDirectory()&&!f.name.startsWith('.'));
async function safeRead(root,relative){const base=await fs.realpath(root),file=await fs.realpath(path.join(root,relative));if(!file.startsWith(base+path.sep))throw Error('Source escapes its library');return fs.readFile(file);}
export async function localSiteEntries(root){
 const entries=[],skipped=[],references=new Set();
 for(const folder of await dirs(path.join(root,'articles'))){
  try{const article=await load(path.join(root,'articles',folder.name,'article.json'));
   if(!['full','guide'].includes(article.kind)||article.published===false||article.deletedAt)continue;
   if(article.production&&!['ready','delivered-local'].includes(article.production.status))continue;
   entries.push({id:'curated:local:'+article.id,category:'curated',title:article.title,href:article.href,format:'html',bytes:await safeRead(path.join(root,'dist'),article.href),guard:{kind:'local',path:'articles/'+folder.name+'/article.json',hash:sha(await fs.readFile(path.join(root,'articles',folder.name,'article.json')))}});
   const indexPath=path.join(root,'articles',folder.name,'reading-index.json');
   let reading;try{reading=await load(indexPath);}catch(e){if(e.code!=='ENOENT')throw e;}
   const guard={kind:'local',path:'articles/'+folder.name+'/article.json',hash:sha(await fs.readFile(path.join(root,'articles',folder.name,'article.json')))};
   for(const concept of reading?.concepts||[])entries.push({id:'explanation:'+article.id+':'+concept.id,category:'knowledge',title:concept.title,href:article.href+'#'+concept.anchor,format:'html',bytes:Buffer.from('<article>'+concept.html+'</article>'),guard});
   for(const ref of reading?.resources||[]){let url;try{url=new URL(ref.url);}catch{continue;}if(!/(^|\.)(arxiv\.org|openreview\.net|aclanthology\.org|proceedings\.mlr\.press|papers\.nips\.cc|proceedings\.neurips\.cc)$/.test(url.hostname)||references.has(ref.url))continue;references.add(ref.url);entries.push({id:'reference:'+sha(ref.url),category:'papers',title:ref.title,href:ref.url,format:'md',metadataOnly:true,bytes:Buffer.from('# '+ref.title+'\n\n论文索引条目：尚未下载全文，不代表已阅读论文。\n'+ref.url),guard});}
  }catch(error){skipped.push({source:folder.name,reason:error.message});}
 }
 for(const folder of await dirs(path.join(root,'knowledge'))){
  try{const concept=await load(path.join(root,'knowledge',folder.name,'concept.json'));if(concept.status!=='accepted'||concept.deletedAt)continue;
   entries.push({id:'knowledge:local:'+folder.name,category:'knowledge',title:concept.title||concept.nameZh||folder.name,href:'knowledge.html',format:'md',bytes:await safeRead(path.join(root,'knowledge',folder.name),'content.md'),guard:{kind:'local',path:'knowledge/'+folder.name+'/concept.json',hash:sha(await fs.readFile(path.join(root,'knowledge',folder.name,'concept.json')))}});
  }catch(error){skipped.push({source:folder.name,reason:error.message});}
 }
 for(const folder of await dirs(path.join(root,'papers'))){
  try{const paper=await load(path.join(root,'papers',folder.name,'paper.json'));if(paper.deletedAt||paper.placement==='about')continue;
   entries.push({id:'paper:local:'+paper.id,category:'papers',title:paper.title,href:'papers.html',format:'pdf',bytes:await safeRead(path.join(root,'papers',folder.name),paper.contentFile||'document.pdf'),guard:{kind:'local',path:'papers/'+folder.name+'/paper.json',hash:sha(await fs.readFile(path.join(root,'papers',folder.name,'paper.json')))}});
  }catch(error){skipped.push({source:folder.name,reason:error.message});}
 }
 return {entries,skipped};
}
export async function cloudSiteEntries(store,accountId){
 const entries=[],skipped=[];const get=key=>store.get(key,{type:'json'});
 // Published index is authoritative; never export another account's drafts.
 for(const {key} of (await store.list({prefix:'workspaces/'})).blobs){
  const index=(await store.getMetadata(key))?.metadata?.indexKey;if(!index)continue;
  const articles=await get(index)||[];if(!articles.length)continue;
  const blob=await store.get(key,{type:'arrayBuffer'});if(!blob)continue;const files=JSON.parse(gunzipSync(Buffer.from(blob)));
  for(const article of articles){
   try{if(!article.publication?.revision||article.deletedAt)continue;
    const name='articles/'+article.id+'/revisions/'+article.publication.revision+'/content.md';if(!files[name])throw Error('Published revision missing');
    entries.push({id:'blog:cloud:'+article.id,article_id:article.id,category:'blogs',title:article.title,href:article.href||article.id+'.html',format:'md',bytes:Buffer.from(files[name],'base64'),guard:{kind:'blog',workspace:key,id:article.id,revision:article.publication.revision}});
   }catch(error){skipped.push({source:article.id,reason:error.message});}
  }
 }
 for(const {key} of (await store.list({prefix:'curated-public/'})).blobs){
  const marker=await get(key);if(!marker)continue;const jobKey='curated/'+marker.owner+'/articles/'+marker.id+'/article.json',job=await get(jobKey);if(!job?.publication)continue;
  const draft=job.publication.draft;
  const body=[job.source?.body,...(draft?.units||[]).map(u=>u.translation),JSON.stringify(draft?.metadata||{})].filter(Boolean).join('\n\n');
  entries.push({id:'curated:cloud:'+job.id,category:'curated',title:job.source?.title||job.title||job.id,href:job.id+'.html',format:'md',bytes:Buffer.from(body),guard:{kind:'curated',key:jobKey,publicationHash:sha(JSON.stringify(job.publication))}});
 }
 const owners=new Set(accountId?[accountId]:[]);
 for(const prefix of ['public-knowledge/','public-papers/'])for(const {key} of (await store.list({prefix})).blobs){const p=await get(key);if(p?.ownerId)owners.add(p.ownerId);}
 for(const ownerId of owners){
  const library=await get('personal-library/'+ownerId);for(const item of library?.items||[]){
   if(item.deletedAt||!['knowledge','pdf'].includes(item.kind)||(ownerId!==accountId&&item.visibility!=='public'))continue;
   const file=item.files.find(f=>item.kind==='pdf'?f.type==='application/pdf':/\.md$/i.test(f.path));if(!file){skipped.push({source:item.id,reason:'No readable Markdown or PDF source'});continue;}
   try{const parts=[];for(let i=0;i<file.parts;i++){const data=await store.get('personal-data/'+ownerId+'/'+file.sha256+'/'+i,{type:'arrayBuffer'});if(!data)throw Error('Missing file part');parts.push(Buffer.from(data));}const bytes=Buffer.concat(parts);if(sha(bytes)!==file.sha256)throw Error('Source checksum mismatch');
    entries.push({id:'library:'+ownerId+':'+item.id,category:item.kind==='pdf'?'papers':'knowledge',title:item.title,href:item.visibility==='public'&&item.publicId?'library-reader.html?'+(item.kind==='pdf'?'paper=':'knowledge=')+item.publicId:'library-reader.html?id='+item.id,format:item.kind==='pdf'?'pdf':'md',bytes,guard:{kind:'library',ownerId,id:item.id,revision:item.revision,sha256:file.sha256,private:ownerId===accountId&&item.visibility!=='public'}});
   }catch(error){skipped.push({source:item.id,reason:error.message});}
  }
 }
 return {entries,skipped};
}
export async function permitted(entry,{root,store,accountId}){
 const guard=entry.guard;
 try{
  if(guard.kind==='local')return sha(await safeRead(root,guard.path))===guard.hash;
  if(!store)return false;
  if(guard.kind==='library'){const library=await store.get('personal-library/'+guard.ownerId,{type:'json'}),item=library?.items.find(i=>i.id===guard.id);return !!item&&!item.deletedAt&&(guard.ownerId===accountId||item.visibility==='public')&&item.revision===guard.revision&&item.files.some(f=>f.sha256===guard.sha256);}
  if(guard.kind==='curated'){const job=await store.get(guard.key,{type:'json'});return !!job?.publication&&sha(JSON.stringify(job.publication))===guard.publicationHash;}
  if(guard.kind==='blog'){const key=(await store.getMetadata(guard.workspace))?.metadata?.indexKey;const list=key?await store.get(key,{type:'json'}):[];return !!list?.some(a=>a.id===guard.id&&!a.deletedAt&&a.publication?.revision===guard.revision);}
 }catch{return false;}
 return false;
}
export async function exportSiteSources({root,store,accountId,destination=path.join(root,'.readless/research-site')}){
 const local=await localSiteEntries(root),cloud=store?await cloudSiteEntries(store,accountId):{entries:[],skipped:[]};await fs.mkdir(destination,{recursive:true,mode:0o700});
 const entries={};for(const entry of [...local.entries,...cloud.entries]){const key=sha(entry.id).slice(0,32)+'.'+entry.format;await fs.writeFile(path.join(destination,key+'.tmp'),entry.bytes,{mode:0o600});await fs.rename(path.join(destination,key+'.tmp'),path.join(destination,key));const {bytes,...metadata}=entry;entries[key]={...metadata,sha256:sha(bytes)};}
 const manifest={updatedAt:new Date().toISOString(),entries,skipped:[...local.skipped,...cloud.skipped]};await atomic(path.join(destination,'manifest.json'),manifest);await refreshSiteAccess({root,store,accountId,destination});return {counts:Object.values(entries).reduce((out,e)=>(out[e.category]=(out[e.category]||0)+1,out),{}),skipped:manifest.skipped};
}
async function atomic(file,value){const temp=file+'.'+randomUUID()+'.tmp';await fs.writeFile(temp,JSON.stringify(value),{mode:0o600});await fs.rename(temp,file);}
export async function refreshSiteAccess({root,store,accountId,destination=path.join(root,'.readless/research-site')}){
 let manifest;try{manifest=await load(path.join(destination,'manifest.json'));}catch(e){if(e.code==='ENOENT')return;throw e;}
 const checked=await Promise.all(Object.entries(manifest.entries).map(async([file,entry])=>await permitted(entry,{root,store,accountId})?file:null));const enabled=checked.filter(Boolean);
 await atomic(path.join(destination,'access.json'),{checkedAt:Date.now(),enabled});return {enabled:enabled.length};
}
