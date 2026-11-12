export function initResearchFolders({request,dialog,remote}){
 const panel=document.getElementById('research-folders');if(!panel)return ()=>{};
 if(remote){panel.hidden=true;document.getElementById('research-site-sources')?.setAttribute('hidden','');return ()=>{};}
 const form=document.getElementById('research-folder-form'),input=document.getElementById('research-folder-path'),list=document.getElementById('research-folder-list'),status=document.getElementById('research-folder-status');let timer,busy=false,siteTimer;
 const siteButton=document.getElementById('research-site-refresh'),siteStatus=document.getElementById('research-site-status');
 async function refreshSite(){if(!siteButton)return;try{const value=await request('/site-sources');siteButton.disabled=value.status==='indexing';const labels={curated:'精选解读',blogs:'原创博客',knowledge:'知识',papers:'论文'};const counts=Object.entries((value.status==='ready'?value.report?.sources:value.sync?.counts)||{}).map(([key,n])=>`${labels[key]||key} ${n} 份`).join(' · ');siteStatus.textContent=value.status==='indexing'?'正在同步网站内容并建立索引…':value.status==='ready'?`已就绪 · ${counts} · ${value.report?.chunks||0} 个片段${value.report?.skipped?.length?' · '+value.report.skipped.length+' 份未纳入':''}`:value.status==='failed'?`${value.error}${counts?' 已同步：'+counts:''}`:'尚未建立网站资料索引。';clearTimeout(siteTimer);if(value.status==='indexing'&&dialog.open)siteTimer=setTimeout(refreshSite,2000);}catch(error){siteStatus.textContent=error.message;}}
 if(siteButton)siteButton.onclick=async()=>{siteButton.disabled=true;try{await request('/site-sources',{method:'POST',body:{}});await refreshSite();}catch(error){siteStatus.textContent=error.message;siteButton.disabled=false;}};
 const node=(tag,text)=>{const n=document.createElement(tag);n.textContent=text;return n;};
 function render(data){list.replaceChildren();busy=data.folders.some(f=>f.status==='indexing');form.querySelector('button').disabled=busy;
  for(const folder of data.folders){const item=node('li','');item.append(node('strong',folder.name),node('p',folder.path));const report=folder.report;
   item.append(node('p',folder.status==='indexing'?'正在扫描和建立索引…':folder.status==='failed'?folder.error:`已就绪 · ${report?.files||0} 份文件 · ${report?.chunks||0} 个片段${folder.updatedAt?' · '+new Date(folder.updatedAt).toLocaleString():''}`));
   if(report?.skipped?.length){const details=node('details','');details.append(node('summary',`${report.skipped.length} 份文件未纳入`));for(const entry of report.skipped)details.append(node('p',`${entry.path}：${entry.reason}`));item.append(details);}
   for(const [action,label] of [['refresh','刷新索引'],['remove','移除']]){const button=node('button',label);button.type='button';button.className='research-action';button.disabled=busy;button.onclick=()=>change({action,id:folder.id});item.append(button);}list.append(item);
  }
  if(!data.folders.length)list.append(node('li','尚未添加。现有笔记库和 ReadLess 博客仍按原设置参与检索。'));
  clearTimeout(timer);if(busy&&dialog.open)timer=setTimeout(refresh,2000);
 }
 async function refresh(){try{render(await request('/folders'));}catch(error){status.textContent=error.message;}}
 async function change(body){form.querySelector('button').disabled=true;status.textContent='正在处理…';try{render(await request('/folders',{method:'POST',body}));if(body.action==='add')input.value='';status.textContent=body.action==='remove'?'已移除，原文件未修改。':'索引在后台进行，可关闭设置。就绪后用于后续研究。';}catch(error){status.textContent=error.message;await refresh();}}
 form.addEventListener('submit',event=>{event.preventDefault();if(!busy)change({action:'add',path:input.value.trim()});});dialog.addEventListener('close',()=>{clearTimeout(timer);clearTimeout(siteTimer);});return ()=>{refresh();refreshSite();};
}
