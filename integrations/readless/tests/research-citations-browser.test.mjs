import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {build} from 'esbuild';
import {chromium} from 'playwright';

const sections=['第一节','第二节'].map((heading,index)=>({heading,paragraphs:Array.from({length:3},(_,i)=>({paragraph_id:`p${index}${i}`,text:`第 ${index+1} 节的第 ${i+1} 段，包含**加粗内容**。`,block_type:'paragraph'}))}));
const citations=sections.flatMap(section=>section.paragraphs.map(p=>({paragraph_id:p.paragraph_id,ordinal:1,evidence_id:'e1',start_char:0,end_char:[...p.text].length})));
citations.push({...citations[1],ordinal:2,evidence_id:'e2'});
const research={runId:'run',status:'completed',memoryStatus:'completed',report:{structured:{schema_version:3,title:'引用展示',sections},citations},bundle:{sources:[1,2].map(i=>({id:'s'+i,title:'来源 '+i,canonical_url:'https://example.org/'+i})),evidence:[1,2].map(i=>({id:'e'+i,source_id:'s'+i,quote:'原文证据'})),relations:[]}};
const source=`import {createResearchController} from './src/research-agent-view.js';const message={id:'message',research:${JSON.stringify(research)}};const controller=createResearchController({store:{get:()=>({messages:[message]})},getActiveId:()=> 'chat'});controller.render();document.getElementById('rerender').onclick=()=>controller.render();`;
const script=(await build({stdin:{contents:source,resolveDir:process.cwd()},bundle:true,format:'esm',write:false})).outputFiles[0].text;
const css=await fs.readFile('src/research.css','utf8');
for(const width of [390,1280])test(`citations are sparse inline superscripts and sources start collapsed at ${width}px`,async t=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});t.after(()=>browser.close());const page=await browser.newPage({viewport:{width,height:900}});
 await page.route('http://127.0.0.1:4180/**',r=>new URL(r.request().url()).pathname==='/app.js'?r.fulfill({body:script,contentType:'text/javascript'}):r.fulfill({body:'<meta charset="utf-8"><style>'+css+'</style><button id="rerender">刷新展示</button><article data-message-id="message"></article><script type="module" src="/app.js"></script>',contentType:'text/html'}));
 await page.goto('http://127.0.0.1:4180/');await page.locator('.research-source-summary').waitFor();
 assert.equal(await page.locator('.research-block-citations sup').count(),3);
 assert.equal(await page.locator('.research-markdown').first().locator('sup').count(),0);
 assert(await page.locator('.research-block-citations').evaluateAll(nodes=>nodes.every(node=>node.parentElement.tagName==='P')));
 assert(await page.locator('.research-block-citations sup').evaluateAll(nodes=>nodes.every(node=>getComputedStyle(node).verticalAlign==='super')));
 assert.equal(await page.locator('.research-source-summary').evaluate(el=>el.open),false);
 assert.equal(await page.getByRole('link',{name:'来源 1',exact:true}).isVisible(),false);
 await page.getByText('来源',{exact:true}).click();assert.equal(await page.getByRole('link',{name:'来源 1',exact:true}).isVisible(),true);
 await page.getByRole('button',{name:'刷新展示',exact:true}).click();assert.equal(await page.locator('.research-source-summary').evaluate(el=>el.open),true);
 await page.getByText('来源',{exact:true}).click();assert.equal(await page.locator('.research-source-summary').evaluate(el=>el.open),false);
});
