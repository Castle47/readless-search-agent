import {renderResearchBlock} from './research-markdown.js';
import {researchRequest,downloadResearch,RESEARCH_API,TERMINAL_STATUSES} from './research-api.js';
import {sectionParagraphs,paragraphText,reportText,sectionCitationGroups,primaryIdentityForEvidence,exampleLabelRequired,researchExecutionSummary} from './research-report.js';
import {renderDraftMarkdown} from './research-draft.js';
import {citationPresentation,representationLabel} from './research-source-groups.js';
const phaseNames={queued:'已排队',understand:'理解问题',recall:'查阅研究记忆',discover:'确认研究对象',quality:'检查解释与行文',planning:'拆分研究问题',peer_review:'研究者交叉核对',research:'检索与核对来源',merge:'合并证据',supplemental:'补充研究',write:'撰写报告',validate_report:'核验报告',completed:'研究完成',partial:'回答待完善',rejected:'需要调整问题',failed:'研究未完成',cancelled:'已取消',running:'研究中',recovering:'正在恢复研究'};
const node=(tag,text,cls)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;};
function safeLink(url,label){try{const parsed=new URL(url);if(parsed.hostname==='readless.invalid')return node('span',label);if(!['https:','http:'].includes(parsed.protocol))return node('span',label);const a=node('a',label);a.href=parsed.href;a.target='_blank';a.rel='noopener noreferrer';return a;}catch{return node('span',label);}}
function citationMark(url,ordinal){const mark=node('sup',undefined,'research-citation');mark.append(safeLink(url,`[${ordinal}]`));return mark;}
export function researchContext(chat,messageId){
 const previous=chat.messages.slice(0,chat.messages.findIndex(m=>m.id===messageId)).slice(-4);
 return previous.map(message=>{
  const research=message.research;
  const urls=[...new Set((research?.bundle?.sources||[]).map(source=>source.canonical_url))];
  let answer='';
  if(research?.draft)answer='上次实验回答（尚未通过完整验收，事实需重新核验）：'+research.draft.markdown;
  else if(research?.unverifiedDraft&&research.status!=='completed')answer='上次初稿（尚未通过检验，事实需重新核验）：'+research.unverifiedDraft.draft.sections.flatMap(section=>(section.paragraphs||[]).map(p=>p.text)).join('\n');
  else if(research?.report)answer='上次回答（作为对话背景，事实仍需核验）：'+reportText(research.report);
  return `用户：${message.content}\n${answer}${urls.length?'\n上次来源线索：'+urls.join(' '):''}`;
 }).join('\n\n').slice(-10000);
}

export function createResearchController({store,getActiveId,refreshHistory,scrollToLatest,showSettings}){
 const pending=new Set();let polling=false;
 function card(message){
  if(!message)return;
  const article=document.querySelector(`[data-message-id="${CSS.escape(message.id)}"]`);if(!article)return;
  const sourcesExpanded=article.querySelector('details.research-source-summary')?.open===true;
  article.querySelector('.research-answer')?.remove();
  if(!message.research)return;
  const r=message.research,box=node('section',undefined,'research-answer');box.setAttribute('aria-label','研究助手');
  if(r.draft){
   box.append(node('p',r.draft.label+' · 尚未通过完整验收','research-run-state'));
   box.append(node('p',new Date(r.draft.created_at).toLocaleString()+' · '+Number(r.draft.tokens||0).toLocaleString()+' tokens','research-run-detail'));
   box.append(renderDraftMarkdown(r.draft.markdown));
   const download=node('a','下载原始 Markdown','research-action');download.href=`${RESEARCH_API}/drafts/${encodeURIComponent(r.draft.id)}/report.md`;download.download=`research-draft-${r.draft.id}.md`;download.onclick=async event=>{event.preventDefault();try{await downloadResearch(`/drafts/${encodeURIComponent(r.draft.id)}/report.md`,download.download);}catch(error){box.append(node('p',error.message,'research-run-error'));}};box.append(download);
   article.append(box);return;
  }
  const state=node('p',phaseNames[r.status]||phaseNames[r.phase]||(r.status==='submitting'?'正在提交研究…':'研究助手'),'research-run-state');
  if(r.status&&!TERMINAL_STATUSES.has(r.status)&&r.phase)state.textContent=phaseNames[r.phase]||phaseNames[r.status]||'研究中';
  box.append(state);
  if(r.error){box.append(node('p',r.error,'research-run-error'));const retry=node('button','重试提交','research-action');retry.type='button';retry.onclick=()=>submit(getActiveId(),message.id);if(!r.runId)box.append(retry);}
  if(r.runId&&!TERMINAL_STATUSES.has(r.status)){
   const details=node('p',r.coverage?`必需研究维度已覆盖 ${Math.round(r.coverage*100)}%`:'已提交的研究会在后台继续执行。','research-run-detail');box.append(details);
   const cancel=node('button',r.cancelRequested?'正在取消…':'停止研究','research-action');cancel.type='button';cancel.disabled=!!r.cancelRequested;
   cancel.onclick=async()=>{cancel.disabled=true;try{await researchRequest(`/runs/${r.runId}/cancel`,{method:'POST',body:{}});store.updateResearch(getActiveId(),message.id,{cancelRequested:true});}catch(error){cancel.disabled=false;details.textContent=error.message;}};box.append(cancel);
  }
  const executionSummary=researchExecutionSummary(r);if(executionSummary)box.append(node('p',executionSummary,'research-run-detail'));
  if(r.status==='rejected')box.append(node('p',r.stopReason==='ambiguous_query'?'请补充研究对象和必要背景，再发送问题。':'研究助手目前支持需要检索证据的问题，请说明希望研究的主题。'));
  if(r.status==='failed')box.append(node('p','任务未能可靠完成，已取得的资料仍保存在本机。'+(r.failureCode?`（${r.failureCode}）`:'')));
  if(r.status==='partial'&&['budget_exhausted','deadline'].includes(r.stopReason))box.append(node('p',r.stopReason==='budget_exhausted'?'本次额度不足，未能完成整篇回答的核验。下方仅显示已核验的部分。':'本次达到运行时限，整篇回答的核验尚未完成。下方仅显示已核验的部分。','research-run-detail'));
  if(r.status==='partial'&&r.hasUnverifiedDraft){const inspect=node('button','查看未完成核验的初稿','research-action');inspect.type='button';inspect.onclick=async()=>{inspect.disabled=true;try{const value=await researchRequest(`/runs/${r.runId}/draft`),dialog=node('dialog',undefined,'research-source-dialog'),close=node('button','关闭','research-action');close.onclick=()=>dialog.close();dialog.append(close,node('p',value.label,'research-run-state'),node('h2',value.draft.title));for(const section of value.draft.sections){if(section.heading)dialog.append(node('h3',section.heading));for(const block of section.paragraphs)dialog.append(renderResearchBlock(block));}dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);dialog.showModal();}catch(error){inspect.textContent=error.message;}finally{inspect.disabled=false;}};box.append(inspect);}
  const showUnverified=!!r.unverifiedDraft && (r.status!=='completed'||!r.report);
  if(showUnverified){
   box.append(node('p',TERMINAL_STATUSES.has(r.status)?'没有通过检验 · 以下为已生成的回答，请结合来源核对。':'初稿 · 研究与核验仍在进行，请结合来源核对。','research-run-error'));
   const draft=r.unverifiedDraft.draft;
   if(draft.title)box.append(node('h2',draft.title));
   for(const section of draft.sections||[]){
    if(section.heading)box.append(node('h3',section.heading));
    for(const paragraph of section.paragraphs||[])box.append(renderResearchBlock(paragraph));
   }
  }
  if(r.report&&!showUnverified){
   const report=r.report,sourceMap=new Map((r.bundle?.sources||[]).map(s=>[s.id,s])),evidenceMap=new Map((r.bundle?.evidence||[]).map(e=>[e.id,e]));
   const presentation=citationPresentation(report,r.bundle),displayCitations=presentation.citations;
   box.append(node('h2',report.structured.title));
   if(!r.unverifiedDraft&&report.structured.schema_version===3&&!report.structured.sections.some(s=>sectionParagraphs(s).length)){
    const reason=r.stopReason||report.execution?.stop_reason;box.append(node('p',reason==='budget_exhausted'?'本次运行额度已用完，未能完成综合回答。已获取的资料仍保存在研究记录中。':reason==='deadline'?'本次运行达到时间上限，未能完成综合回答。已获取的资料仍保存在研究记录中。':'本次研究未能完成可靠的综合回答，已获取的资料仍保存在研究记录中。'));
    if(sourceMap.size){const materials=node('details',undefined,'research-sources');materials.append(node('summary','查看已获取的资料'));for(const source of sourceMap.values()){const p=node('p');p.append(safeLink(source.canonical_url,source.title));materials.append(p);}box.append(materials);}
   }
   for(const section of report.structured.sections){
    const blocks=sectionParagraphs(section);if(!blocks.length)continue;if(section.heading)box.append(node('h3',section.heading));
    const citationGroups=sectionCitationGroups(blocks,displayCitations);
    for(const [blockIndex,statement] of blocks.entries()){
     const paragraph=renderResearchBlock({...statement,text:paragraphText(statement)});
     if(exampleLabelRequired(statement))paragraph.prepend(node('small','示意','research-example-label'));
     const marks=node('span',undefined,'research-block-citations');
     for(const citation of citationGroups[blockIndex]){
      const source=sourceMap.get(evidenceMap.get(citation.evidence_id)?.source_id);
      if(source)marks.append(citationMark(source.canonical_url,citation.ordinal));
     }
     if(marks.childNodes.length){
      const anchors=paragraph.querySelectorAll('p,li,h1,h2,h3,h4,h5,h6,td,th');
      const anchor=anchors[anchors.length-1];
      if(anchor)anchor.append(document.createTextNode('\u2060'),marks);
      else{marks.classList.add('research-code-citations');paragraph.append(marks);}
     }
     box.append(paragraph);
    }
   }
   // Coverage and audit diagnostics remain in the JSON export, not the answer.
   const evidence=node('details',undefined,'research-source-summary');evidence.open=sourcesExpanded;evidence.append(node('summary','来源'));const sourceList=node('ul');evidence.append(sourceList);const seen=new Set(),sourceGroups=new Map(),workItems=new Map();
   for(const citation of displayCitations){
    if(seen.has(citation.evidence_id))continue;seen.add(citation.evidence_id);const e=evidenceMap.get(citation.evidence_id),s=sourceMap.get(e?.source_id);if(!e||!s)continue;
    if(!sourceGroups.has(s.id)){const group=presentation.bySource.get(s.id),key=group?.key||s.id;let item=workItems.get(key);if(!item){item=node('li');item.append(citationMark(s.canonical_url,citation.ordinal),document.createTextNode(' '),safeLink(s.canonical_url,s.title));if(group?.sources.length>1){const views=node('p');for(const view of group.sources)views.append(safeLink(view.canonical_url,representationLabel(view)),document.createTextNode(' '));item.append(views);}sourceList.append(item);workItems.set(key,item);}const details=node('details');details.append(node('summary',group?.sources.length>1?representationLabel(s)+' · 原文证据':'原文证据'),node('small',`发布／更新：${s.published_at||'未知'} · 抓取：${s.retrieved_at}`));item.append(details);sourceGroups.set(s.id,details);}
    const block=node('div',undefined,'research-evidence');block.append(node('blockquote',e.quote));
    const identity=primaryIdentityForEvidence(e.id,r.bundle?.relations);
    if(identity){block.append(node('p',`一手来源身份依据：${identity.publisher_name}。${identity.quote||identity.link_label||''}`,'research-run-detail'));if(identity.link_url){const origin=sourceMap.get(identity.support_source_id);if(origin)block.append(safeLink(origin.canonical_url,'查看官方链接的出处'));}}
    const snapshot=node('button','查看本次原文快照','research-action');snapshot.type='button';snapshot.onclick=async()=>{snapshot.disabled=true;try{const source=await researchRequest(`/runs/${r.runId}/sources/${s.id}`);const dialog=node('dialog',undefined,'research-source-dialog');const close=node('button','关闭','research-action');close.onclick=()=>dialog.close();const sourceText=node('pre'),characters=[...source.content_text],selected=characters.slice(e.start_char,e.end_char).join('');let mark;
    if(selected===e.quote){mark=node('mark',selected);sourceText.append(document.createTextNode(characters.slice(0,e.start_char).join('')),mark,document.createTextNode(characters.slice(e.end_char).join('')));}else{sourceText.textContent=source.content_text;}
    const toolbar=node('div',undefined,'research-source-toolbar');toolbar.append(node('strong','原文快照'),close);dialog.append(toolbar,node('h2',source.title),node('p',`SHA-256：${source.content_sha256}`),sourceText);dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);dialog.showModal();if(mark)mark.scrollIntoView({block:'center'});}catch(error){snapshot.textContent=error.message;}finally{snapshot.disabled=false;}};
    block.append(snapshot);sourceGroups.get(s.id).append(block);
   }
   if(sourceGroups.size)box.append(evidence);const downloads=node('div',undefined,'research-downloads');
   for(const [suffix,label] of [['report.md','下载 Markdown'],['result','导出 JSON']]){const a=node('a',label,'research-action');a.href=`${RESEARCH_API}/runs/${r.runId}/${suffix}`;a.download=`research-${r.runId}.${suffix==='result'?'json':'md'}`;a.onclick=async event=>{event.preventDefault();try{await downloadResearch(`/runs/${r.runId}/${suffix}`,a.download);}catch(error){downloads.append(node('p',error.message,'research-run-error'));}};downloads.append(a);}box.append(downloads);
   if(['degraded','pending','needs_reconciliation','dead_letter'].includes(r.memoryStatus))box.append(node('p',r.memoryStatus==='applied'?'研究记忆已更新。':'报告已保存；长期记忆'+({degraded:'暂时不可用',pending:'等待更新',needs_reconciliation:'正在核对写入状态',dead_letter:'更新失败，可稍后检查'}[r.memoryStatus]||'待处理')+'。','research-run-detail'));
  }
  article.append(box);
 }
 async function submit(chatId,messageId){
  if(pending.has(messageId))return;pending.add(messageId);
  try{
   let chat=store.get(chatId),message=chat?.messages.find(m=>m.id===messageId);if(!message)return;
   if(!message.research?.request)store.updateResearch(chatId,messageId,{request:{query:message.content,context:researchContext(chat,messageId),output_language:'zh-CN',memory_mode:'read_write'},status:'submitting'});
   message=store.get(chatId).messages.find(m=>m.id===messageId);card(message);
   const result=await researchRequest('/runs',{method:'POST',body:message.research.request,key:messageId});
   store.updateResearch(chatId,messageId,{runId:result.run_id,status:result.status,error:null});
  }catch(error){store.updateResearch(chatId,messageId,{status:'submission_failed',error:error.message});if(error.code==='setup_required')showSettings();}
  finally{pending.delete(messageId);const message=store.get(chatId)?.messages.find(m=>m.id===messageId);if(getActiveId()===chatId&&message)card(message);refreshHistory();}
 }
 async function poll(){
  if(polling||document.hidden)return;polling=true;
  try{
   const chatId=getActiveId(),chat=store.get(chatId);if(!chat)return;
   for(const message of chat.messages){
    const r=message.research;if(!r)continue;
    if(r.status==='submitting'&&!r.runId){await submit(chatId,message.id);continue;}
    const memoryPending=['pending','needs_reconciliation'].includes(r.memoryStatus);
    if(!r.runId||TERMINAL_STATUSES.has(r.status)&&(r.report||!['completed','partial'].includes(r.status))&&!memoryPending&&(r.status==='completed'||r.draftChecked))continue;
    try{
     const state=await researchRequest(`/runs/${r.runId}`);
     const patch={status:state.status,phase:state.phase,coverage:state.coverage,budget:state.budget,memoryStatus:state.memory_status,stopReason:state.stop_reason,hasUnverifiedDraft:!!state.has_unverified_draft,draftChecked:TERMINAL_STATUSES.has(state.status),failureCode:state.error?.code,error:null,cancelRequested:state.cancel_requested};
     if(state.has_unverified_draft&&state.status!=='completed'&&(!r.unverifiedDraft||state.status!==r.status||state.phase!==r.phase))patch.unverifiedDraft=await researchRequest(`/runs/${r.runId}/draft`);
     if(state.report_id&&!r.report){patch.report=await researchRequest(`/runs/${r.runId}/result`);patch.bundle=await researchRequest(`/runs/${r.runId}/evidence?limit=200`);}
     if(JSON.stringify({...r,...patch})!==JSON.stringify(r)){const updated=store.updateResearch(chatId,message.id,patch);if(getActiveId()===chatId)card(updated);}
    }catch(error){const updated=store.updateResearch(chatId,message.id,{error:error.message});if(getActiveId()===chatId)card(updated);}
   }
  }finally{polling=false;}
 }
 setInterval(poll,2000);window.addEventListener('online',poll);document.addEventListener('visibilitychange',poll);
 return {submit,render(){const chat=store.get(getActiveId());for(const message of chat?.messages||[])card(message);poll();}};
}
