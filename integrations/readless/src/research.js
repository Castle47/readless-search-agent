import {createChatStore,chatTitle,CHAT_KEY} from './research-store.js';
import {initResearchSettings,initResearchArchive,openResearchDraft,openResearchRun,createResearchController,initResearchConnection,namespaceStorage,storageKey} from './research-runtime.js';

const byId=id=>document.getElementById(id);
const input=byId('chat-input'),send=byId('send-message'),messages=byId('chat-messages'),history=byId('chat-history'),workspace=byId('research-workspace'),toggle=byId('toggle-history'),status=byId('chat-status');
let storage;
const connection=initResearchConnection();
if(connection.remote){const values=new Map();storage={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)};}
else try{storage=window.localStorage;}catch{storage={getItem(){throw new Error('unavailable');}};}
storage=namespaceStorage(storage);
const store=createChatStore({storage,onError:text=>{status.textContent=text;}});
const drafts=new Map();
const narrow=matchMedia('(max-width:760px)');
let activeId=null,composing=false;
let historyMode='active',openActions=null;
let agent;
function closeActions(restoreFocus=false){
  if(!openActions)return;
  const {element,trigger}=openActions;element.hidden=true;trigger.setAttribute('aria-expanded','false');openActions=null;
  if(restoreFocus&&trigger.isConnected)trigger.focus();
}
function showActions(element,trigger){
  if(openActions?.trigger===trigger){closeActions();return;}
  closeActions();element.hidden=false;trigger.setAttribute('aria-expanded','true');openActions={element,trigger};
  const rect=trigger.getBoundingClientRect();
  element.style.left=Math.max(8,Math.min(rect.right+6,window.innerWidth-element.offsetWidth-8))+'px';
  element.style.top=Math.max(8,Math.min(rect.top,window.innerHeight-element.offsetHeight-8))+'px';
}
function resizeInput(){input.style.height='auto';input.style.height=Math.min(input.scrollHeight,180)+'px';send.disabled=!input.value.trim()||(connection.remote&&connection.state!=='ready');}
window.addEventListener('research-availability-change',resizeInput);
function setSidebar(open){
  closeActions();
  workspace.dataset.sidebar=open?'open':'closed';
  toggle.setAttribute('aria-expanded',String(open));
  toggle.setAttribute('aria-label',open?'收起历史对话':'展开历史对话');
  byId('history-backdrop').hidden=!(open&&narrow.matches);
}
function renderHistory(){
  closeActions();document.querySelectorAll('.research-chat-actions').forEach(el=>el.remove());
  const archived=historyMode==='archived',chats=store.list({archived});
  history.replaceChildren();byId('history-empty').hidden=chats.length>0;
  byId('history-empty').textContent=archived?'还没有归档对话。':'从一个问题开始，每次对话都会留在这里。';
  byId('show-chat-history').setAttribute('aria-pressed',String(!archived));
  byId('show-archived-chats').setAttribute('aria-pressed',String(archived));
  history.setAttribute('aria-label',archived?'已归档对话':'历史对话');
  for(const chat of chats){
    const row=document.createElement('div');row.className='research-chat-row';row.dataset.active=String(chat.id===activeId);
    const button=document.createElement('button');button.type='button';button.className='research-chat-title';button.textContent=chatTitle(chat);button.title=chatTitle(chat);
    if(chat.id===activeId)button.setAttribute('aria-current','true');
    button.addEventListener('click',()=>{drafts.set(activeId,input.value);openChat(chat.id);if(narrow.matches)setSidebar(false);input.focus();});
    const more=document.createElement('button');more.type='button';more.className='research-chat-more';more.setAttribute('aria-label','对话操作：'+chatTitle(chat));
    const dots=document.createElementNS('http://www.w3.org/2000/svg','svg');dots.setAttribute('viewBox','0 0 24 24');dots.setAttribute('aria-hidden','true');dots.setAttribute('focusable','false');
    for(const x of [5,12,19]){const dot=document.createElementNS('http://www.w3.org/2000/svg','circle');dot.setAttribute('cx',String(x));dot.setAttribute('cy','12');dot.setAttribute('r','1.8');dots.append(dot);}
    more.append(dots);
    const actions=document.createElement('div');actions.className='research-chat-actions';actions.id='chat-actions-'+history.childElementCount;actions.hidden=true;actions.setAttribute('role','menu');actions.setAttribute('aria-label','对话操作');more.setAttribute('aria-haspopup','menu');
    more.setAttribute('aria-controls',actions.id);more.setAttribute('aria-expanded',String(!actions.hidden));
    more.addEventListener('click',()=>showActions(actions,more));
    more.addEventListener('keydown',e=>{if(['ArrowDown','ArrowRight'].includes(e.key)){e.preventDefault();if(actions.hidden)showActions(actions,more);actions.querySelector('button').focus();}});
    const archive=document.createElement('button');archive.type='button';archive.textContent=archived?'恢复对话':'归档';
    archive.addEventListener('click',()=>{
      try{
        const changed=archived?store.restore(chat.id):store.archive(chat.id);if(!changed){renderHistory();return;}
        closeActions();
        if(!archived&&activeId===chat.id){drafts.set(activeId,input.value);openChat(null);}else renderHistory();
        status.textContent=archived?'已恢复到历史对话。':'已归档，可在“已归档”中查看或恢复。';
        byId(archived?'show-archived-chats':'show-chat-history').focus();
      }catch(error){status.textContent=error.message;}
    });
    const remove=document.createElement('button');remove.type='button';remove.className='research-chat-delete';remove.textContent='删除';
    remove.addEventListener('click',()=>{
      try{
        store.remove(chat.id);drafts.delete(chat.id);closeActions();
        if(activeId===chat.id)openChat(null);else renderHistory();
        status.textContent='对话已删除。';byId(historyMode==='archived'?'show-archived-chats':'show-chat-history').focus();
      }catch(error){status.textContent=error.message;}
    });
    archive.setAttribute('role','menuitem');remove.setAttribute('role','menuitem');
    actions.append(archive,remove);row.append(button,more);history.append(row);document.body.append(actions);
  }
}
for(const [id,mode] of [['show-chat-history','active'],['show-archived-chats','archived']])byId(id).addEventListener('click',()=>{historyMode=mode;renderHistory();});
document.addEventListener('pointerdown',e=>{if(openActions&&!openActions.element.contains(e.target)&&!openActions.trigger.contains(e.target))closeActions();});
document.addEventListener('focusin',e=>{if(openActions&&!openActions.element.contains(e.target)&&!openActions.trigger.contains(e.target))closeActions();});
history.addEventListener('scroll',()=>closeActions());window.addEventListener('resize',()=>closeActions());
function messageElement(message){
  const article=document.createElement('article');article.className='research-message';article.dataset.messageId=message.id;
  const label=document.createElement('span');label.className='research-message-label';label.textContent='你';
  const text=document.createElement('p');text.className='research-message-text';text.textContent=message.content;
  article.append(label,text);return article;
}
function scrollToLatest(){requestAnimationFrame(()=>{const area=byId('chat-scroll');area.scrollTop=area.scrollHeight;});}
function setRoute(chatId){const url=new URL(location.href);url.searchParams.delete('draft');url.searchParams.delete('run');url.searchParams.delete('label');if(chatId)url.searchParams.set('chat',chatId);else url.searchParams.delete('chat');window.history.replaceState(null,'',url);}
function openChat(chatId){
  const chat=chatId?store.get(chatId):null;activeId=chat?.id||null;
  if(chat)historyMode=chat.archivedAt?'archived':'active';
  messages.replaceChildren(...(chat?.messages||[]).map(messageElement));
  byId('chat-welcome').hidden=!!chat?.messages.length;
  input.value=drafts.get(activeId)||'';resizeInput();renderHistory();setRoute(activeId);scrollToLatest();
  agent?.render();
  if(chat?.messages.length===1&&(chat.messages[0].research?.draft||chat.messages[0].research?.report))requestAnimationFrame(()=>{byId('chat-scroll').scrollTop=0;});
}
function newChat(){drafts.set(activeId,input.value);drafts.delete(null);historyMode='active';openChat(null);if(narrow.matches)setSidebar(false);input.focus();}
byId('new-chat').addEventListener('click',newChat);byId('new-chat-shortcut').addEventListener('click',newChat);
toggle.addEventListener('click',()=>setSidebar(workspace.dataset.sidebar!=='open'));
for(const id of ['close-history','history-backdrop'])byId(id).addEventListener('click',()=>{setSidebar(false);toggle.focus();});
document.addEventListener('keydown',e=>{
  if(openActions&&['ArrowDown','ArrowUp','Home','End'].includes(e.key)&&openActions.element.contains(e.target)){
    e.preventDefault();const items=[...openActions.element.querySelectorAll('button')],index=items.indexOf(document.activeElement);
    items[e.key==='Home'?0:e.key==='End'?items.length-1:(index+(e.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();return;
  }
  if(e.key!=='Escape')return;
  if(openActions){e.preventDefault();closeActions(true);return;}
  if(narrow.matches&&workspace.dataset.sidebar==='open'){setSidebar(false);toggle.focus();}
});
function syncViewport(){setSidebar(!narrow.matches);}
narrow.addEventListener('change',syncViewport);
input.addEventListener('input',resizeInput);
input.addEventListener('compositionstart',()=>{composing=true;});input.addEventListener('compositionend',()=>{composing=false;resizeInput();});
input.addEventListener('keydown',event=>{
  if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing&&!composing&&event.keyCode!==229&&!narrow.matches){event.preventDefault();byId('chat-form').requestSubmit();}
});
byId('chat-form').addEventListener('submit',event=>{
  event.preventDefault();if(composing)return;
  if(connection.remote&&connection.state!=='ready'){status.textContent='研究助手暂时离线或尚未登录，输入的内容已保留。';return;}
  try{
    const chat=store.send(activeId,input.value);if(!chat)return;
    drafts.delete(activeId);activeId=chat.id;historyMode='active';setRoute(activeId);
    // Reconcile persisted messages (including other tabs) without replacing announced ones.
    const existing=new Set([...messages.children].map(node=>node.dataset.messageId));
    for(const message of chat.messages)if(!existing.has(message.id))messages.append(messageElement(message));
    byId('chat-welcome').hidden=true;input.value='';resizeInput();renderHistory();scrollToLatest();input.focus();
    agent.submit(chat.id,chat.messages.at(-1).id);
  }catch(error){status.textContent=error.message;}
});
window.addEventListener('storage',event=>{
  if(connection.remote)return;
  if(event.key!==storageKey&&event.key!==null)return;
  drafts.set(activeId,input.value);openChat(activeId);
});
const showSettings=initResearchSettings();
initResearchArchive(store,chatId=>{drafts.set(activeId,input.value);openChat(chatId);if(narrow.matches)setSidebar(false);});
agent=createResearchController({store,getActiveId:()=>activeId,refreshHistory:renderHistory,scrollToLatest,showSettings});
const initialRoute=new URL(location.href),requestedDraft=initialRoute.searchParams.get('draft'),requestedRun=initialRoute.searchParams.get('run');
syncViewport();openChat(initialRoute.searchParams.get('chat'));
if(requestedDraft){
  status.textContent='正在读取实验回答…';
  openResearchDraft(store,requestedDraft).then(chat=>{drafts.set(activeId,input.value);openChat(chat.id);status.textContent='';}).catch(error=>{status.textContent=error.message;});
}else if(requestedRun){
  status.textContent='正在读取研究记录…';
  openResearchRun(store,requestedRun,{label:initialRoute.searchParams.get('label')||''}).then(chat=>{drafts.set(activeId,input.value);openChat(chat.id);status.textContent='';}).catch(error=>{status.textContent=error.message;});
}
