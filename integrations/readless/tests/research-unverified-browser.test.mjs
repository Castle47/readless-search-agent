import test from 'node:test';import assert from 'node:assert/strict';import {build} from 'esbuild';import {chromium} from 'playwright';
for(const status of ['running','partial'])test(`${status} research automatically displays its unverified answer without opening a dialog`,async t=>{
 const source=`import {createResearchController} from './src/research-agent-view.js';
 const message={id:'message',content:'Kev?',research:{runId:'run',status:'${status}',memoryStatus:'completed',report:{structured:{schema_version:3,title:'Kev',sections:[]},citations:[]}}};
 const chat={messages:[message]};const store={get:()=>chat,updateResearch:(chatId,id,patch)=>{Object.assign(message.research,patch);return message;}};
 createResearchController({store,getActiveId:()=> 'chat',refreshHistory:()=>{},scrollToLatest:()=>{},showSettings:()=>{}}).render();`;
 const bundle=(await build({stdin:{contents:source,resolveDir:process.cwd()},bundle:true,format:'esm',write:false})).outputFiles[0].text;
 const browser=await chromium.launch({channel:'chrome',headless:true});t.after(()=>browser.close());const page=await browser.newPage();
 await page.route('http://127.0.0.1:4180/**',r=>{const path=new URL(r.request().url()).pathname;
  if(path==='/')return r.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><article data-message-id="message"></article><script type="module">'+bundle+'</script>'});
  if(path.endsWith('/draft'))return r.fulfill({json:{label:'尚未通过检验',draft:{title:'Kev 的推理加速',sections:[{heading:'减少开销',paragraphs:[{text:'通过算子融合减少启动开销。',block_type:'paragraph'}]}]}}});
  return r.fulfill({json:{status,has_unverified_draft:true,memory_status:'completed'}});
 });
 await page.goto('http://127.0.0.1:4180/');await page.getByText('通过算子融合减少启动开销。',{exact:true}).waitFor();
 assert.equal(await page.getByText(status==='partial'?/没有通过检验 ·/:/初稿 · 研究与核验仍在进行/).isVisible(),true);
 assert.equal(await page.locator('dialog[open]').count(),0);
});
