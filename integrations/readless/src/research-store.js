export const CHAT_KEY='readless.research.chats.v1';
export const MAX_MESSAGE_LENGTH=8000;
const blank=()=>({version:1,chats:[]});
export function parseChatState(raw){
  if(raw===null)return blank();
  const state=JSON.parse(raw);
  if(state?.version!==1||!Array.isArray(state.chats))throw new Error('Unsupported chat history');
  const ids=new Set();
  for(const chat of state.chats){
    if(!chat||typeof chat.id!=='string'||ids.has(chat.id)||!Array.isArray(chat.messages)||typeof chat.createdAt!=='string'||typeof chat.updatedAt!=='string')throw new Error('Invalid chat history');
    ids.add(chat.id);
    if(chat.archivedAt!==undefined&&typeof chat.archivedAt!=='string')throw new Error('Invalid archive state');
    for(const message of chat.messages)if(!message||message.role!=='user'||typeof message.content!=='string'||!message.content.trim()||typeof message.id!=='string'||typeof message.createdAt!=='string')throw new Error('Invalid message');
  }
  return state;
}
export function chatTitle(chat){const first=chat.messages[0],label=first.research?.deliveryLabel||first.research?.draft?.label;return [...((label?label+' · ':'')+first.content.replace(/\s+/g,' ').trim())].slice(0,32).join('');}
export function createChatStore({storage,onError=()=>{},now=()=>new Date().toISOString(),id=()=>crypto.randomUUID()}={}){
  let state=blank(),volatile=false;
  function read(){
    if(volatile)return;
    try{state=parseChatState(storage.getItem(CHAT_KEY));}
    catch{volatile=true;onError('暂时无法读取历史对话。本次问题仍可记录，但仅在当前页面保留。');}
  }
  function save(){
    if(volatile)return;
    try{storage.setItem(CHAT_KEY,JSON.stringify(state));}
    catch{volatile=true;onError('问题已显示，但未能保存到浏览器。关闭页面后，本次新增内容可能丢失。');}
  }
  read();
  // Management actions must persist before changing the visible history.
  function manage(chatId,change){
    read();if(!state.chats.some(c=>c.id===chatId))return false;
    if(volatile)throw new Error('当前无法保存聊天更改，请恢复浏览器存储后重试。');
    const next=structuredClone(state);change(next);
    try{storage.setItem(CHAT_KEY,JSON.stringify(next));}
    catch{throw new Error('更改未保存，原聊天仍保留。请检查浏览器存储后重试。');}
    state=next;return true;
  }
  return {
    list({archived=false,includeArchived=false}={}){read();return structuredClone(state.chats).filter(c=>c.messages.length&&(includeArchived||Boolean(c.archivedAt)===archived)).sort((a,b)=>b.updatedAt.localeCompare(a.updatedAt));},
    get(chatId){read();return structuredClone(state.chats.find(c=>c.id===chatId)||null);},
    archive(chatId){return manage(chatId,next=>{const chat=next.chats.find(c=>c.id===chatId);chat.archivedAt=now();});},
    restore(chatId){return manage(chatId,next=>{delete next.chats.find(c=>c.id===chatId).archivedAt;});},
    remove(chatId){return manage(chatId,next=>{next.chats=next.chats.filter(c=>c.id!==chatId);});},
    importDraft(draft){
      if(draft?.kind!=='experimental_answer'||typeof draft.id!=='string'||!/^[a-z0-9][a-z0-9-]*$/.test(draft.id)||typeof draft.markdown!=='string'||typeof draft.query!=='string'||!draft.query.trim()||!/^[a-f0-9]{64}$/.test(draft.sha256))throw new Error('实验回答格式无效。');
      read();
      const existing=state.chats.find(c=>c.messages.some(m=>m.research?.draft?.id===draft.id&&m.research.draft.sha256===draft.sha256));
      if(existing)return structuredClone(existing);
      const timestamp=now(),chat={id:id(),createdAt:timestamp,updatedAt:timestamp,messages:[{id:id(),role:'user',content:draft.query,createdAt:timestamp,research:{status:'experimental',draft:structuredClone(draft)}}]};
      state.chats.push(chat);save();return structuredClone(chat);
    },
    send(chatId,content){
      if(typeof content!=='string'||!content.trim())return null;
      const text=content.trim();
      if([...text].length>MAX_MESSAGE_LENGTH)throw new Error('问题过长，请控制在 8,000 字符内。');
      read();
      const timestamp=now();
      let chat=state.chats.find(c=>c.id===chatId);
      if(!chat){chat={id:id(),createdAt:timestamp,updatedAt:timestamp,messages:[]};state.chats.push(chat);}
      delete chat.archivedAt;
      chat.messages.push({id:id(),role:'user',content:text,createdAt:timestamp});
      chat.updatedAt=timestamp;save();return structuredClone(chat);
    },
    updateResearch(chatId,messageId,patch){
      read();const chat=state.chats.find(c=>c.id===chatId),message=chat?.messages.find(m=>m.id===messageId);
      if(!message)return null;
      message.research={...message.research,...structuredClone(patch)};save();return structuredClone(message);
    }
  };
}
