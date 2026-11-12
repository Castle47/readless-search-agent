import {createSiteManager} from './research-site-manager.mjs';
import {createResearchFolders} from './research-folders.mjs';
import {readFile, mkdir, open, rename, chmod} from 'node:fs/promises';
import path from 'node:path';
import {randomBytes} from 'node:crypto';
import {isLocalRequest} from './local-request.mjs';
import {listResearchDrafts,readResearchDraft} from './research-drafts.mjs';

export const RESEARCH_PREFIX='/__readless/research';
export const RESEARCH_MODEL='openai/gpt-6-luna';
export const researchSecretsFile=root=>path.join(root,'.readless/research-config/secrets.json');
export async function readResearchSecrets(root){
  try{return JSON.parse(await readFile(researchSecretsFile(root),'utf8'));}
  catch(error){if(error.code!=='ENOENT')throw error;}
  try{return JSON.parse(await readFile(path.join(root,'.readless/research-secrets.json'),'utf8'));}
  catch(error){if(error.code==='ENOENT')return {};throw error;}
}
export async function saveResearchSecrets(root,patch){
  const dir=path.join(root,'.readless/research-config');await mkdir(dir,{recursive:true,mode:0o700});
  const current=await readResearchSecrets(root);
  const next={...current,...patch,model:RESEARCH_MODEL,api_bearer_token:current.api_bearer_token||randomBytes(32).toString('hex')};
  const file=researchSecretsFile(root),tmp=file+'.'+randomBytes(6).toString('hex');
  const handle=await open(tmp,'wx',0o600);
  try{await handle.writeFile(JSON.stringify(next));await handle.sync();}finally{await handle.close();}
  await rename(tmp,file);await chmod(file,0o600);
  return next;
}
async function body(req){
  const chunks=[];let bytes=0;
  for await(const chunk of req){bytes+=chunk.length;if(bytes>65536)throw Object.assign(new Error('请求过大。'),{status:413});chunks.push(chunk);}
  try{return JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{throw Object.assign(new Error('请求格式错误。'),{status:400});}
}
export function createResearchHttp({root,port,backend='http://127.0.0.1:8000',fetchImpl=fetch,folderOptions={},siteOptions={}}){
  const folders=createResearchFolders(root,folderOptions);
  const site=createSiteManager(root,siteOptions);
  const json=(res,code,value)=>{res.writeHead(code,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(JSON.stringify(value));};
  return async function handle(req,res,route){
    if(route!==RESEARCH_PREFIX&&!route.startsWith(RESEARCH_PREFIX+'/'))return false;
    if(!isLocalRequest(req,port)){req.resume();json(res,403,{error:{code:'local_only',message:'研究助手仅供本机使用。'}});return true;}
    if(!['GET','HEAD'].includes(req.method)&&(![`http://127.0.0.1:${port}`,`http://localhost:${port}`].includes(req.headers.origin)||!String(req.headers['content-type']||'').startsWith('application/json'))){
      req.resume();json(res,403,{error:{code:'invalid_origin',message:'请从本机研究助手页面提交。'}});return true;
    }
    try{
      if(route===RESEARCH_PREFIX+'/site-sources'){
        if(req.method==='GET')json(res,200,await site.status());
        else if(req.method==='POST'){const secrets=await readResearchSecrets(root);json(res,202,await site.index(secrets.research_account_id));}
        else json(res,405,{error:{message:'不支持此操作。'}});
        return true;
      }
      if(route===RESEARCH_PREFIX+'/folders'){
        if(req.method==='GET')json(res,200,await folders.list());
        else if(req.method==='POST')json(res,202,await folders.change(await body(req)));
        else json(res,405,{error:{message:'不支持此操作。'}});
        return true;
      }
      const draftRoute=route.slice(RESEARCH_PREFIX.length);
      if(draftRoute==='/drafts'||draftRoute.startsWith('/drafts/')){
        if(req.method!=='GET'){req.resume();json(res,405,{error:{code:'method_not_allowed',message:'实验回答仅支持查看。'}});return true;}
        if(draftRoute==='/drafts'){json(res,200,{drafts:await listResearchDrafts(root)});return true;}
        const match=draftRoute.match(/^\/drafts\/([a-z0-9][a-z0-9-]*)(\/report\.md)?$/);
        if(!match){json(res,404,{error:{code:'not_found',message:'找不到这份实验回答。'}});return true;}
        const draft=await readResearchDraft(root,match[1]);
        if(match[2]){
          res.writeHead(200,{'Content-Type':'text/markdown; charset=utf-8','Content-Disposition':`attachment; filename="research-draft-${draft.id}.md"`,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});
          res.end(draft.markdown);
        }else json(res,200,draft);
        return true;
      }
      if(route===RESEARCH_PREFIX+'/settings'){
        if(req.method==='PUT'){
          const input=await body(req);
          if(!input||Object.keys(input).some(k=>k!=='api_key')||typeof input.api_key!=='string'||!/^sk-or-[A-Za-z0-9_-]{16,500}$/.test(input.api_key.trim()))throw Object.assign(new Error('请输入有效的 OpenRouter API key。'),{status:422});
          await saveResearchSecrets(root,{openrouter_api_key:input.api_key.trim()});
          json(res,200,{configured:true,model:RESEARCH_MODEL});return true;
        }
        if(req.method!=='GET'){json(res,405,{error:{code:'method_not_allowed',message:'不支持此操作。'}});return true;}
        const secrets=await readResearchSecrets(root);
        let readiness={ready:false,message:'研究服务尚未启动。'};
        try{const response=await fetchImpl(backend+'/readyz',{signal:AbortSignal.timeout(2500)});const value=await response.json();readiness={...value,ready:response.ok};}catch{}
        json(res,200,{configured:!!secrets.openrouter_api_key,model:readiness.model||RESEARCH_MODEL,service:readiness});return true;
      }
      const suffix=route.slice(RESEARCH_PREFIX.length);
      if(!/^\/runs(?:\/[0-9a-f-]{36}(?:\/(?:result|report\.md|draft|evidence|events|cancel|sources\/[0-9a-f-]{36}))?)?$/.test(suffix)){json(res,404,{error:{code:'not_found',message:'找不到此研究入口。'}});return true;}
      const validMethod=suffix==='/runs'?['GET','POST']:suffix.endsWith('/cancel')?['POST']:['GET'];
      if(!validMethod.includes(req.method)){json(res,405,{error:{code:'method_not_allowed',message:'不支持此操作。'}});return true;}
      const secrets=await readResearchSecrets(root);
      if(!secrets.api_bearer_token){json(res,503,{error:{code:'setup_required',message:'请先在研究设置中配置 API key。'}});return true;}
      if(suffix==='/runs'&&req.method==='POST')await site.access(secrets.research_account_id);
      const url=new URL(req.url,'http://localhost');
      const target=new URL('/v1/research'+suffix.slice('/runs'.length),backend);
      for(const name of ['after_seq','cursor','limit','task_id'])if(url.searchParams.has(name))target.searchParams.set(name,url.searchParams.get(name));
      const headers={'Authorization':'Bearer '+secrets.api_bearer_token};
      if(req.headers['idempotency-key'])headers['Idempotency-Key']=String(req.headers['idempotency-key']).slice(0,200);
      let data;
      if(req.method==='POST'){data=JSON.stringify(await body(req));headers['Content-Type']='application/json';}
      const response=await fetchImpl(target,{method:req.method,headers,body:data,signal:AbortSignal.timeout(15000)});
      res.writeHead(response.status,{'Content-Type':response.headers.get('content-type')||'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});
      res.end(Buffer.from(await response.arrayBuffer()));
    }catch(error){json(res,error.status||503,{error:{code:error.status?'invalid_request':'service_unavailable',message:error.status?error.message:'研究服务暂时不可用，已提交的任务会保留。'}});}
    return true;
  };
}
