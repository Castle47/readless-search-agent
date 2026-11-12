import {build} from 'esbuild';
import {readFile,writeFile,mkdir,rm} from 'node:fs/promises';
import {favicon} from './site-brand.mjs';
import {outputPath,outputDirectory} from './output.mjs';
import {researchMode} from './research-mode.mjs';

const icon=(path)=>`<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
const plus=icon('<path d="M12 5v14M5 12h14"/>');
const panel=icon('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>');
const arrow=icon('<path d="M12 19V5m-6 6 6-6 6 6"/>');
const back=icon('<path d="M19 12H5m6-6-6 6 6 6"/>');

export async function buildResearchPage({output=outputDirectory,mode=researchMode()}={}){
  if(!['local','placeholder'].includes(mode))throw Error('Invalid research mode');
  const outputFile=name=>outputPath(name,output);
  const live=['research-connection.js','research-api.js','research-report.js','research-agent-view.js','research-draft.js','research-source-groups.js','research-markdown.js','research-folders.js'];
  await mkdir(outputFile('assets'),{recursive:true});
  if(mode==='placeholder')for(const name of live)await rm(outputFile('assets/'+name),{force:true});
  const runtime=mode==='local'?'research-runtime-local.js':'research-runtime-placeholder.js';
  await writeFile(outputFile('assets/research-runtime.js'),await readFile(new URL('../src/'+runtime,import.meta.url)));
  // Local code also falls back to the placeholder when opened off loopback.
  if(mode==='local')await writeFile(outputFile('assets/research-runtime-placeholder.js'),await readFile(new URL('../src/research-runtime-placeholder.js',import.meta.url)));
  const css=(await Promise.all(['home.css','site-chrome.css','research.css'].map(name=>readFile(new URL('../src/'+name,import.meta.url),'utf8')))).join('\n');
  await mkdir(outputFile('assets'),{recursive:true});
  for(const name of ['research.js','research-store.js',...(mode==='local'?live:[])])await writeFile(outputFile('assets/'+name),await readFile(new URL('../src/'+name,import.meta.url)));
  if(mode==='local'){
    const bundle=await build({entryPoints:[new URL('../src/research-markdown.js',import.meta.url).pathname],bundle:true,format:'esm',write:false,minify:true,target:'es2022'});
    await writeFile(outputFile('assets/research-markdown.js'),bundle.outputFiles[0].contents);
  }
  const body=`<div class="research-workspace" id="research-workspace">
    <aside class="research-sidebar" id="research-sidebar" aria-label="聊天历史">
      <div class="research-sidebar-top"><a class="research-icon-button" href="index.html" aria-label="返回首页">${back}</a><button type="button" class="research-icon-button" id="close-history" aria-label="收起历史对话">${panel}</button></div>
      <button type="button" class="research-new" id="new-chat">${plus}<span>新建聊天</span></button>
      <button type="button" id="research-archive-open" class="research-archive-button">本机研究记录</button>
      <div class="research-history-tabs" role="group" aria-label="对话列表"><button type="button" id="show-chat-history" aria-pressed="true">历史对话</button><button type="button" id="show-archived-chats" aria-pressed="false">已归档</button></div>
      <nav id="chat-history" class="research-history" aria-label="历史对话"></nav>
      <p class="research-history-empty" id="history-empty">从一个问题开始，<br>每次对话都会留在这里。</p>
      <div class="research-sidebar-footer"><button type="button" id="research-settings-open" class="research-settings-button">研究设置</button><p class="research-local-note">${mode==='local'?'研究保存在本机服务<br>聊天目录保存在当前浏览器':'对话仅保存在当前浏览器<br>大模型 API 尚未接入'}</p></div>
    </aside>
    <button type="button" class="research-sidebar-backdrop" id="history-backdrop" aria-label="关闭历史对话" hidden></button>
    <main id="main" class="research-main">
      <header class="research-topbar"><div><a class="research-icon-button research-main-back" href="index.html" aria-label="返回首页">${back}</a><button type="button" class="research-icon-button" id="toggle-history" aria-label="展开历史对话" aria-controls="research-sidebar" aria-expanded="true">${panel}</button><h1>研究助手</h1></div><button type="button" id="research-connection-open" class="research-action" hidden>连接本机</button><button type="button" class="research-icon-button research-new-shortcut" id="new-chat-shortcut" aria-label="新建聊天">${plus}</button></header>
      <p id="research-availability" class="research-availability" role="status" hidden></p><div class="research-scroll" id="chat-scroll">
        <section class="research-welcome" id="chat-welcome"><img src="assets/readless-squirrel.svg" width="86" height="86" alt=""><h2>今天，想研究些什么？</h2><p>从一个问题开始，把好奇心继续下去。</p></section>
        <section class="research-messages" id="chat-messages" role="log" aria-label="对话区域" aria-live="polite" aria-relevant="additions" tabindex="0"></section>
      </div>
      <div class="research-composer-area">
        <form class="research-composer" id="chat-form"><label for="chat-input" class="sr-only">输入你的问题</label><textarea id="chat-input" placeholder="有什么想研究的问题？" rows="1" maxlength="8000"></textarea><button type="submit" id="send-message" class="research-send" aria-label="发送问题" disabled>${arrow}</button></form>
        <p id="chat-status" class="research-status" role="status" aria-live="polite"></p>
      </div>
    </main>
  </div><dialog id="research-settings" class="research-settings"><form id="research-settings-form"><div class="research-settings-heading"><h2>研究设置</h2><button type="button" id="research-settings-close" class="research-icon-button" aria-label="关闭研究设置">×</button></div><p id="research-model-label">正在读取模型配置…</p><div id="research-key-fields"><label for="research-api-key">OpenRouter API key</label><input id="research-api-key" type="password" autocomplete="off" spellcheck="false" placeholder="sk-or-…"><p class="research-settings-note">密钥仅保存在本机服务的私有配置中，保存后不会再显示。</p><button class="research-settings-save" type="submit">保存密钥</button></div><p id="research-settings-status" role="status"></p></form>${mode==='local'?`<section id="research-site-sources"><h3>网站资料</h3><p class="research-settings-note">检索精选解读、已发布原创博客，以及知识与论文。公开资料和已绑定账号自己的私有资料可用；不读取其他人的私有内容。更新后请刷新索引。</p><button type="button" id="research-site-refresh" class="research-action">刷新网站资料索引</button><p id="research-site-status" role="status"></p></section><section id="research-folders" aria-labelledby="research-folders-title"><h3 id="research-folders-title">本地资料文件夹</h3><p class="research-settings-note">添加文件夹后，Markdown、TXT 与文字型 PDF 将用于后续研究的本地检索。支持子文件夹；文件变动后请刷新索引。原文件不会被修改，扫描版 PDF 暂不支持。</p><p class="research-settings-note">索引保存在本机；命中的原文片段会发送给你配置的研究模型。移除后不再用于新检索，已有对话中的引用仍保留。</p><form id="research-folder-form"><label for="research-folder-path">文件夹完整路径</label><input id="research-folder-path" placeholder="例如 /Users/你的用户名/Documents/研究资料" autocomplete="off" required><button type="submit" class="research-action">添加并建立索引</button></form><p class="research-settings-note">最多 10 个文件夹；每次最多 1000 份文件、256 MiB，单文件最多 50 MiB。隐藏目录与符号链接不会读取。</p><p id="research-folder-status" role="status"></p><ul id="research-folder-list"></ul></section>`:''}</dialog>`;
  await writeFile(outputFile('research.html'),`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src ${mode==='local'?"'self'":"'none'"}; base-uri 'none'; form-action 'none'"><meta name="description" content="ReadLess 研究助手：从问题开始，保留每一次研究对话。"><title>研究助手 · ReadLess</title>${favicon}<style>${css}</style></head><body class="research-page" data-research-mode="${mode}"><a class="skip-link" href="#main">跳到对话</a>${body}<script type="module" src="assets/research.js"></script></body></html>`);
}
