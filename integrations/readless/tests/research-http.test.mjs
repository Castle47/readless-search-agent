import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm,stat,readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {createResearchHttp,saveResearchSecrets,RESEARCH_MODEL} from '../scripts/research-http.mjs';

test('research secret settings stay private and reject remote/cross-origin writes',async t=>{
 const root=await mkdtemp(path.join(tmpdir(),'readless-research-'));
 let handler;
 const server=http.createServer((req,res)=>handler(req,res,new URL(req.url,'http://localhost').pathname));
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const port=server.address().port;
 handler=createResearchHttp({root,port,fetchImpl:async()=>{throw new Error('offline')}});
 t.after(async()=>{await new Promise(resolve=>server.close(resolve));await rm(root,{recursive:true,force:true});});
 const url=`http://127.0.0.1:${port}/__readless/research/settings`,key='sk-or-test-'+ 'x'.repeat(30);
 let response=await fetch(url,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key})});
 assert.equal(response.status,403);
 response=await fetch(url,{method:'PUT',headers:{'Content-Type':'application/json',Origin:`http://127.0.0.1:${port}`},body:JSON.stringify({api_key:key})});
 assert.equal(response.status,200);
 assert.ok(!(await response.text()).includes(key));
 const settings=await (await fetch(url)).json();assert.equal(settings.configured,true);assert.equal(settings.model,RESEARCH_MODEL);assert.equal(settings.service.ready,false);
 assert.ok(!JSON.stringify(settings).includes(key));
 const file=path.join(root,'.readless/research-config/secrets.json');assert.equal((await stat(file)).mode&0o777,0o600);
 const stored=JSON.parse(await readFile(file));assert.equal(stored.openrouter_api_key,key);assert.ok(stored.api_bearer_token.length>=64);
 const token=stored.api_bearer_token;await saveResearchSecrets(root,{openrouter_api_key:key+'1'});assert.equal(JSON.parse(await readFile(file)).api_bearer_token,token);
 const forwarded=await fetch(url,{headers:{'X-Forwarded-For':'127.0.0.1'}});assert.equal(forwarded.status,403);
});

test('folder management requires same-origin local access and never opens remote paths',async t=>{
 const root=await mkdtemp(path.join(tmpdir(),'readless-folders-http-'));let handler;
 const server=http.createServer((req,res)=>handler(req,res,new URL(req.url,'http://localhost').pathname));await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const port=server.address().port;
 handler=createResearchHttp({root,port});t.after(async()=>{await new Promise(resolve=>server.close(resolve));await rm(root,{recursive:true,force:true});});const origin=`http://127.0.0.1:${port}`,url=origin+'/__readless/research/folders';
 assert.equal((await fetch(url,{headers:{'X-Forwarded-For':'127.0.0.1'}})).status,403);
 assert.equal((await fetch(url,{method:'POST',headers:{Origin:'https://evil.test','Content-Type':'application/json'},body:'{"action":"add","path":"/tmp"}'})).status,403);
 assert.deepEqual(await (await fetch(url)).json(),{folders:[]});
 assert.equal((await fetch(url,{method:'POST',headers:{Origin:origin,'Content-Type':'application/json'},body:'{"action":"add","path":"relative"}'})).status,400);
});
