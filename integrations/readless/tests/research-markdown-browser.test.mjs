import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {build} from 'esbuild';
import {chromium} from 'playwright';

const markdown='# 标题\n\n**粗体**、*斜体*、~~删除线~~、`inline()`。\n\n- 父项目\n  - 子项目\n\n1. 第一项\n2. 第二项\n\n> 引用内容\n\n| A\\|B | 标题 |\n|---|---|\n| 内容 | **强调** |\n\n<script>window.pwned=1</script>\n\n[危险](javascript:alert(1))\n\n![图片](https://outside.test/image.png)';
const table=Array.from({length:8},(_,i)=>'列'+i).join('\t')+'\n'+Array.from({length:8},()=> 'long_value_'.repeat(70)).join('\t');
const code='const value = "'+'x'.repeat(1200)+'";\n<img onerror="window.pwned=1">';
const css=(await Promise.all(['home.css','research.css'].map(n=>fs.readFile('src/'+n,'utf8')))).join('\n');
const script=(await build({stdin:{resolveDir:process.cwd(),contents:`import {renderResearchMarkdown,renderResearchBlock} from './src/research-markdown.js';const box=document.getElementById('answer');box.append(renderResearchMarkdown(${JSON.stringify(markdown)}),renderResearchBlock({block_type:'table',text:${JSON.stringify(table)}}),renderResearchBlock({block_type:'code',text:${JSON.stringify(code)},language:'js'}));`},bundle:true,write:false,format:'esm'})).outputFiles[0].text;
for(const width of [390,1280])test(`research markdown stays safe and scrolls within its blocks at ${width}px`,async t=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});t.after(()=>browser.close());
 const page=await browser.newPage({viewport:{width,height:900}}),unexpected=[];
 await page.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin!=='http://127.0.0.1:4180'){unexpected.push(url.href);return route.abort();}if(url.pathname==='/renderer.js')return route.fulfill({contentType:'text/javascript',body:script});return route.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>'+css+'</style><body class="research-page"><div class="research-workspace" data-sidebar="closed"><main class="research-main"><div class="research-scroll"><div class="research-messages"><article class="research-message"><section id="answer" class="research-answer"></section></article></div></div></main></div><script type="module" src="/renderer.js"></script>'});});
 await page.goto('http://127.0.0.1:4180/');await page.locator('.research-markdown pre').waitFor();
 assert.equal(await page.locator('#answer strong').count(),2);assert.equal(await page.locator('#answer em').count(),1);assert.equal(await page.locator('#answer s').count(),1);
 assert.equal(await page.locator('#answer ul ul').count(),1);assert.equal(await page.locator('#answer ol li').count(),2);assert.equal(await page.locator('#answer blockquote').count(),1);
 assert.equal(await page.locator('#answer table').count(),2);assert.equal(await page.locator('#answer table').first().locator('th').first().innerText(),'A|B');
 assert.equal(await page.locator('#answer script,#answer img,#answer iframe,#answer a[href^="javascript:"]').count(),0);
 assert.equal(await page.evaluate(()=>window.pwned),undefined);assert.deepEqual(unexpected,[]);
 for(const locator of [page.locator('.research-table-scroll').last(),page.locator('.research-markdown pre')]){
  assert(await locator.evaluate(el=>el.scrollWidth>el.clientWidth));await locator.evaluate(el=>{el.scrollLeft=120;});assert(await locator.evaluate(el=>el.scrollLeft>0));
 }
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert(await page.locator('.research-scroll').evaluate(el=>el.scrollWidth<=el.clientWidth+1));
 assert.equal(await page.locator('.research-main').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)');
 await page.screenshot({path:'/tmp/readless-research-markdown-'+width+'.png'});
});
