import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,readFile,writeFile,rm} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {tmpdir} from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {createResearchHttp} from '../scripts/research-http.mjs';
import {draftBlocks,draftInline,renderDraftMarkdown} from '../src/research-draft.js';
import {createChatStore,chatTitle} from '../src/research-store.js';
import {researchContext} from '../src/research-agent-view.js';
import {openResearchRun} from '../src/research-api.js';

test('run deep links import the existing result without submitting a new research request',async t=>{
  const id='20f7cfbc-1c9e-48b0-a58b-2773c86d1266',values=new Map();let next=0,calls=0;
  const store=createChatStore({storage:{getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,v)},id:()=>`chat-${++next}`});
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls++;assert.equal(options.method,'GET');
    const data=url.endsWith('runs?limit=50')?{runs:[{id,query:'原始 Transformer',status:'completed'}]}:url.endsWith('/result')?{markdown:'Existing answer'}:url.includes('/evidence?')?{sources:[]}:{status:'completed',report_id:'existing-report'};
    return {ok:true,json:async()=>data};
  });
  const chat=await openResearchRun(store,id,{label:'Kev 复测'});
  assert.equal(chatTitle(chat),'Kev 复测 · 原始 Transformer');
  assert.equal(chat.messages[0].research.runId,id);assert.equal(chat.messages[0].research.status,'completed');
  assert.equal(chat.messages[0].research.report.markdown,'Existing answer');
  assert.equal((await openResearchRun(store,id)).id,chat.id);assert.equal(calls,4);
  assert.equal(store.list().length,1);
});

test('published drafts are read-only, byte faithful and independent of the model service',async t=>{
  const root=await mkdtemp(path.join(tmpdir(),'readless-draft-'));
  const docs=path.join(root,'research-agent/docs');await mkdir(path.join(docs,'live/example'),{recursive:true});
  const markdown='# Original 🐿️\n\n原样正文。\n';
  const entry={id:'015c',label:'015C 实验回答',query:'原问题',created_at:'2026-09-24T00:00:00Z',file:'example/answer.md',sha256:createHash('sha256').update(markdown).digest('hex'),model:'openai/gpt-5.6-luna',tokens:123};
  await writeFile(path.join(docs,'live/example/answer.md'),markdown);
  const registry=path.join(docs,'answer-previews.json');await writeFile(registry,JSON.stringify({version:1,drafts:[entry]}));
  let handler,backendCalls=0;
  const server=http.createServer((req,res)=>handler(req,res,new URL(req.url,'http://localhost').pathname));
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const port=server.address().port;handler=createResearchHttp({root,port,fetchImpl:async()=>{backendCalls++;throw new Error('must not call backend');}});
  t.after(async()=>{await new Promise(resolve=>server.close(resolve));await rm(root,{recursive:true,force:true});});
  const base=`http://127.0.0.1:${port}/__readless/research/drafts`;
  const list=await (await fetch(base)).json();assert.equal(list.drafts[0].id,'015c');assert.equal(list.drafts[0].kind,'experimental_answer');assert.equal(list.drafts[0].markdown,undefined);assert.equal(list.drafts[0].file,undefined);
  const value=await (await fetch(base+'/015c')).json();assert.equal(value.markdown,markdown);assert.equal(value.kind,'experimental_answer');assert.equal(value.report_id,undefined);assert.equal(value.runId,undefined);
  assert.equal(await (await fetch(base+'/015c/report.md')).text(),markdown);
  assert.equal((await fetch(base+'/other')).status,404);
  assert.equal((await fetch(base+'/015c',{method:'POST',headers:{Origin:`http://127.0.0.1:${port}`,'Content-Type':'application/json'},body:'{}'})).status,405);
  assert.equal((await fetch(base,{headers:{'X-Forwarded-For':'127.0.0.1'}})).status,403);
  await writeFile(path.join(docs,'live/example/answer.md'),'secret changed bytes');
  const altered=await fetch(base+'/015c');assert.equal(altered.status,503);assert.ok(!(await altered.text()).includes('secret changed bytes'));
  await writeFile(registry,JSON.stringify({version:1,drafts:[{...entry,file:'../../private.md'}]}));
  assert.equal((await fetch(base+'/015c')).status,503);assert.equal(backendCalls,0);
});

test('opening a draft preserves existing chats, persists and does not fabricate a research run',()=>{
  const values=new Map();let n=0;const storage={getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,v)};
  const store=createChatStore({storage,id:()=>`id-${++n}`});
  const old=store.send(null,'旧问题');store.updateResearch(old.id,old.messages[0].id,{runId:'old-run',status:'completed',report:{existing:true}});
  const before=store.get(old.id);
  const draft={id:'015c',kind:'experimental_answer',label:'015C 实验回答',query:'介绍一下 Jev 模型',markdown:'最新回答',sha256:'a'.repeat(64)};
  const chat=store.importDraft(draft);assert.deepEqual(store.get(old.id),before);
  assert.equal(store.importDraft(draft).id,chat.id);assert.equal(store.list().length,2);
  const restored=createChatStore({storage}).get(chat.id);assert.equal(restored.messages[0].research.status,'experimental');assert.equal(restored.messages[0].research.runId,undefined);assert.equal(restored.messages[0].research.report,undefined);
  assert.match(chatTitle(restored),/015C/);
  store.send(chat.id,'进一步解释');const followup=store.get(chat.id);assert.match(researchContext(followup,followup.messages[1].id),/尚未通过完整验收/);assert.match(researchContext(followup,followup.messages[1].id),/最新回答/);
});

test('draft rendering preserves code/table content and cannot interpret raw HTML or unsafe links',()=>{
  const raw='# Title\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1)) **strong** [source](https://example.org)\n\n```js\n<img onerror=alert(1)>\nconst n = 0.78;\n```\n\n| A | B |\n|---|---|\n| 8.4 | 0.78 |\n';
  const blocks=draftBlocks(raw);assert.equal(blocks.find(b=>b.type==='code').text,'<img onerror=alert(1)>\nconst n = 0.78;');assert.deepEqual(blocks.find(b=>b.type==='table').rows,[['A','B'],['8.4','0.78']]);
  assert.ok(draftInline('[bad](data:text/html,hello)').every(t=>t.type!=='link'));
});

test('015C table and full worked example remain present in display blocks',async()=>{
  const text=await readFile(new URL('../research-agent/docs/live/quality-v3/015/c/candidate.md',import.meta.url),'utf8');
  const blocks=draftBlocks(text);assert.equal(blocks.filter(b=>b.type==='table').length,1);
  assert.ok(blocks.some(b=>b.type==='code'&&b.text.includes('风险等级 = 8.4')&&b.text.includes('立即隔离概率 = 0.78')));
  assert.ok(blocks.some(b=>b.type==='paragraph'&&b.text.includes('所以系统不会盲目自动隔离')));
  assert.equal(blocks.at(-1).type,'paragraph');assert.ok(blocks.at(-1).text.includes('人工回退机制'));
});
