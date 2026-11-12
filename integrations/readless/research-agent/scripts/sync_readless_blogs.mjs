// Host-only read of this account's original blogs and current drafts into the private RAG cache.
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createRequire} from 'node:module';
import {gunzipSync} from 'node:zlib';
import {fileURLToPath} from 'node:url';
const root=fileURLToPath(new URL('../../',import.meta.url));
const require=createRequire(path.join(root,'work/netlify-serverless/package.json'));
const {getStore}=require('@netlify/blobs');
const accountId=process.argv[2];
if(!/^[a-f0-9-]{36}$/.test(accountId||''))throw Error('Pass the authenticated ReadLess account ID.');
const config=JSON.parse(await fs.readFile(path.join(os.homedir(),'Library/Preferences/netlify/config.json'),'utf8'));
const {siteId}=JSON.parse(await fs.readFile(path.join(root,'work/netlify-serverless/.netlify/state.json'),'utf8'));
const store=getStore({name:'readless-runtime-v1',siteID:siteId,token:config.users[config.userId].auth.token,consistency:'strong'});
const registry=await store.get('auth/users',{type:'json'});
const user=registry.users.find(user=>user.id===accountId);
if(!user)throw Error('Account not found.');
const destination=path.join(root,'.readless/research-cloud-blogs');
await fs.mkdir(destination,{recursive:true,mode:0o700});
const seen=new Set();
for(const owner of [...new Set([user.id,user.legacyBlogWorkspaceId].filter(Boolean))]){
 const blob=await store.get('workspaces/'+owner,{type:'arrayBuffer'});
 if(!blob)continue;
 const files=JSON.parse(gunzipSync(Buffer.from(blob)));
 for(const [name,encoded] of Object.entries(files)){
  const match=name.match(/^articles\/(blog-[a-f0-9-]{36})\/article\.json$/);
  if(!match)continue;
  const article=JSON.parse(Buffer.from(encoded,'base64'));
  if(!article.published||article.deletedAt||article.kind!=='authored')continue;
  const revision=article.published ? article.publication?.revision : article.revision;
  if(!Number.isSafeInteger(revision)||revision<1||article.id!==match[1])throw Error('Invalid publication metadata');
  const contentKey='articles/'+article.id+'/revisions/'+revision+'/content.md';
  if(!files[contentKey])throw Error('Published source missing');
  const metadataKey='articles/'+article.id+'/revisions/'+revision+'/metadata.json';
  const published=files[metadataKey]?JSON.parse(Buffer.from(files[metadataKey],'base64')):{};
  const dir=path.join(destination,article.id),snapshot=path.join(dir,'revisions',String(revision));
  await fs.mkdir(snapshot,{recursive:true,mode:0o700});
  await fs.writeFile(path.join(snapshot,'content.md'),Buffer.from(files[contentKey],'base64'),{mode:0o600});
  const metadata={...article,title:published.title||article.title,ragOrigin:{kind:'readless-cloud',workspaceOwner:owner,sourceKey:contentKey}};
  await fs.writeFile(path.join(dir,'article.json.next'),JSON.stringify(metadata),{mode:0o600});
  await fs.rename(path.join(dir,'article.json.next'),path.join(dir,'article.json'));
  seen.add(article.id);
 }
}
for(const entry of await fs.readdir(destination,{withFileTypes:true})){
 if(entry.isDirectory()&&/^blog-[a-f0-9-]{36}$/.test(entry.name)&&!seen.has(entry.name))await fs.rm(path.join(destination,entry.name),{recursive:true});
}
console.log(JSON.stringify({originalBlogs:seen.size,account:user.displayName}));
