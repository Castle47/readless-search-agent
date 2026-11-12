import fs from 'node:fs/promises';import path from 'node:path';import os from 'node:os';import {createRequire} from 'node:module';import {fileURLToPath} from 'node:url';
import {exportSiteSources,refreshSiteAccess} from '../../scripts/research-site-sources.mjs';
const root=fileURLToPath(new URL('../../',import.meta.url)),accountId=process.argv[2]||null;
if(accountId&&!/^[a-f0-9-]{36}$/.test(accountId))throw Error('Invalid research account');
let store;
try{const require=createRequire(path.join(root,'work/netlify-serverless/package.json'));const {getStore}=require('@netlify/blobs');const config=JSON.parse(await fs.readFile(path.join(os.homedir(),'Library/Preferences/netlify/config.json'),'utf8'));const {siteId}=JSON.parse(await fs.readFile(path.join(root,'work/netlify-serverless/.netlify/state.json'),'utf8'));store=getStore({name:'readless-runtime-v1',siteID:siteId,token:config.users[config.userId].auth.token,consistency:'strong'});
 if(accountId){const registry=await store.get('auth/users',{type:'json'});if(!registry.users.some(u=>u.id===accountId))throw Error('Research account is not registered');}
}catch(error){if(accountId)throw error;}
const fn=process.argv.includes('--access-only')?refreshSiteAccess:exportSiteSources;
console.log(JSON.stringify(await fn({root,store,accountId})));
