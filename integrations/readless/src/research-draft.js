import {renderResearchMarkdown} from './research-markdown.js';
// Read-only presentation for explicitly published experimental answers.
// Raw HTML is always text; this never becomes a verified research report.
export function draftInline(text){
  const result=[],pattern=/(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  let cursor=0;
  for(const match of text.matchAll(pattern)){
    if(match.index>cursor)result.push({type:'text',text:text.slice(cursor,match.index)});
    const token=match[0];
    if(token.startsWith('`'))result.push({type:'code',text:token.slice(1,-1)});
    else if(token.startsWith('**'))result.push({type:'strong',text:token.slice(2,-2)});
    else{
      const parts=token.match(/^\[([^\]]+)\]\((.+)\)$/);
      let href=null;
      try{const url=new URL(parts[2]);if(['http:','https:'].includes(url.protocol))href=url.href;}catch{}
      result.push(href?{type:'link',text:parts[1],href}:{type:'text',text:token});
    }
    cursor=match.index+token.length;
  }
  if(cursor<text.length)result.push({type:'text',text:text.slice(cursor)});
  return result;
}

export function draftBlocks(markdown){
  const lines=markdown.replace(/\r\n?/g,'\n').split('\n'),blocks=[];
  const cells=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(s=>s.trim());
  const delimiter=line=>/^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line||'');
  const listItem=line=>line.match(/^\s*(-|\d+\.)\s+(.+)$/);
  for(let i=0;i<lines.length;){
    const line=lines[i];if(!line.trim()){i++;continue;}
    const fence=line.match(/^\s*(`{3,})(.*)$/);
    if(fence){const code=[];i++;while(i<lines.length&&!new RegExp('^\\s*`{'+fence[1].length+',}\\s*$').test(lines[i]))code.push(lines[i++]);if(i<lines.length)i++;blocks.push({type:'code',language:fence[2].trim(),text:code.join('\n')});continue;}
    const heading=line.match(/^(#{1,6})\s+(.+)$/);
    if(heading){blocks.push({type:'heading',level:Math.min(6,heading[1].length+1),text:heading[2]});i++;continue;}
    if(line.includes('|')&&delimiter(lines[i+1])){
      const rows=[cells(line)];i+=2;while(i<lines.length&&lines[i].trim().startsWith('|'))rows.push(cells(lines[i++]));
      blocks.push({type:'table',rows});continue;
    }
    if(/^>\s?/.test(line)){
      const quote=[];while(i<lines.length&&/^>\s?/.test(lines[i]))quote.push(lines[i++].replace(/^>\s?/,''));
      blocks.push({type:'quote',text:quote.join('\n')});continue;
    }
    const item=listItem(line);
    if(item){const ordered=item[1]!=='-',items=[];while(i<lines.length){const next=listItem(lines[i]);if(!next||(next[1]!=='-')!==ordered)break;items.push(next[2]);i++;}blocks.push({type:'list',ordered,items});continue;}
    const paragraph=[line];i++;
    while(i<lines.length&&lines[i].trim()&&!/^(?:#{1,6}\s|>\s?|\s*`{3,})/.test(lines[i])&&!listItem(lines[i])&&!delimiter(lines[i+1]))paragraph.push(lines[i++]);
    blocks.push({type:'paragraph',text:paragraph.join('\n')});
  }
  return blocks;
}

export function renderDraftMarkdown(markdown,doc=document){
  return renderResearchMarkdown(markdown,doc);
}
