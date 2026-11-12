import {initResearchFolders} from './research-folders.js';
import {researchConnection} from './research-connection.js';
export const RESEARCH_API='/__readless/research';
export const TERMINAL_STATUSES=new Set(['completed','partial','failed','rejected','cancelled','experimental']);
export async function researchRequest(route,{method='GET',body,key,signal}={}){
  return researchConnection().request(RESEARCH_API+route,{method,body,key,signal});
}
export async function downloadResearch(route,filename){
 const response=await researchConnection().request(RESEARCH_API+route,{raw:true});
 const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
export function initResearchSettings(){
  const dialog=document.getElementById('research-settings'),state=document.getElementById('research-settings-status'),key=document.getElementById('research-api-key');
  const refreshFolders=initResearchFolders({request:researchRequest,dialog,remote:researchConnection().remote});
  async function show(){
    refreshFolders();
    key.value='';dialog.showModal();state.textContent='正在检查服务…';
    document.getElementById('research-key-fields').hidden=researchConnection().remote;
    const modelLabel=document.getElementById('research-model-label');
    if(modelLabel)modelLabel.textContent='正在读取模型配置…';
    try{const value=await researchRequest('/settings');
      if(modelLabel){const efforts=[...new Set(Object.values(value.service.reasoning_efforts||{}))];modelLabel.textContent=`模型：${value.model} · OpenRouter${efforts.length===1?' · '+efforts[0]:''}`;}
      state.textContent=[value.configured?'API key 已保存。':'尚未配置 API key。',value.service.ready?'研究服务已就绪。':value.service.message||'研究服务正在配置中。',researchConnection().remote?'模型密钥只能在本机页面修改。':''].join(' ');
    }catch(error){state.textContent=error.message;if(modelLabel)modelLabel.textContent='模型配置暂不可用';}
  }
  document.getElementById('research-settings-open').addEventListener('click',show);
  document.getElementById('research-settings-close').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{key.value='';});
  document.getElementById('research-settings-form').addEventListener('submit',async event=>{
    event.preventDefault();if(researchConnection().remote)return;const button=event.submitter;button.disabled=true;
    try{await researchRequest('/settings',{method:'PUT',body:{api_key:key.value.trim()}});key.value='';state.textContent='API key 已安全保存。';}catch(error){state.textContent=error.message;}finally{button.disabled=false;}
  });
  return show;
}

export async function openResearchDraft(store,id){
  if(!/^[a-z0-9][a-z0-9-]*$/.test(id))throw new Error('找不到这份实验回答。');
  return store.importDraft(await researchRequest('/drafts/'+id));
}

export async function openResearchRun(store,id,{label=''}={}){
  if(!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id))throw new Error('找不到这份研究记录。');
  const existing=store.list({includeArchived:true}).find(c=>c.messages.some(m=>m.research?.runId===id));
  if(existing){
    if(label){
      const message=existing.messages.find(message=>message.research?.runId===id);
      store.updateResearch(existing.id,message.id,{deliveryLabel:String(label).trim().slice(0,40)});
    }
    if(existing.archivedAt)store.restore(existing.id);
    return store.get(existing.id);
  }
  const {runs}=await researchRequest('/runs?limit=50');
  const run=runs.find(r=>r.id===id);
  if(!run)throw new Error('当前本机研究记录中没有找到这份回答。');
  const state=await researchRequest('/runs/'+id);
  const details={deliveryLabel:String(label).trim().slice(0,40),runId:run.id,status:state.status,phase:state.phase,budget:state.budget,stopReason:state.stop_reason,hasUnverifiedDraft:state.has_unverified_draft};
  if(state.report_id){
    const [report,bundle]=await Promise.all([researchRequest(`/runs/${id}/result`),researchRequest(`/runs/${id}/evidence?limit=200`)]);
    Object.assign(details,{report,bundle});
  }
  const chat=store.send(null,run.query);
  store.updateResearch(chat.id,chat.messages[0].id,details);
  return store.get(chat.id);
}

export function initResearchArchive(store,onOpen){
  document.getElementById('research-archive-open').addEventListener('click',async()=>{
    const dialog=document.createElement('dialog');dialog.className='research-settings research-archive';
    const heading=document.createElement('h2');heading.textContent='本机研究记录';
    const close=document.createElement('button');close.type='button';close.textContent='关闭';close.className='research-action';close.onclick=()=>dialog.close();
    const list=document.createElement('div');list.textContent='正在读取…';
    dialog.append(heading,close,list);document.body.append(dialog);dialog.addEventListener('close',()=>dialog.remove());dialog.showModal();
    try{
      const [draftResult,runResult]=await Promise.allSettled([researchRequest('/drafts'),researchRequest('/runs?limit=50')]);list.replaceChildren();
      const records=[...(draftResult.status==='fulfilled'?draftResult.value.drafts.map(d=>({...d,isDraft:true})):[]),...(runResult.status==='fulfilled'?runResult.value.runs:[])].sort((a,b)=>new Date(b.created_at)-new Date(a.created_at));
      if(!records.length&&draftResult.status==='fulfilled'&&runResult.status==='fulfilled')list.textContent='还没有保存的研究。';
      for(const run of records){
        const button=document.createElement('button');button.type='button';button.className='research-archive-item';
        const title=document.createElement('span');title.textContent=run.query+(run.isDraft?' · '+run.label:'');
        const detail=document.createElement('small');detail.textContent=(run.isDraft?'实验回答 · 尚未完整验收':({completed:'已完成',partial:'部分完成',running:'研究中',queued:'已排队',recovering:'恢复中',failed:'未完成',cancelled:'已取消',rejected:'需补充问题'}[run.status]||'已保存'))+' · '+new Date(run.created_at).toLocaleString();
        button.append(title,detail);button.onclick=async()=>{
          if(run.isDraft){button.disabled=true;try{const chat=await openResearchDraft(store,run.id);dialog.close();onOpen(chat.id);}catch(error){detail.textContent=error.message;button.disabled=false;}return;}
          let chat=store.list({includeArchived:true}).find(c=>c.messages.some(m=>m.research?.runId===run.id));
          if(!chat){chat=store.send(null,run.query);store.updateResearch(chat.id,chat.messages[0].id,{runId:run.id,status:run.status});}
          dialog.close();onOpen(chat.id);
        };list.append(button);
      }
      for(const result of [draftResult,runResult])if(result.status==='rejected'){const error=document.createElement('p');error.textContent=result.reason.message;list.append(error);}
    }catch(error){list.textContent=error.message;}
  });
}
