import MarkdownIt from 'markdown-it';

const markdown = new MarkdownIt({html:false, breaks:true, linkify:false});
markdown.validateLink = href => {
  if(href.startsWith('#')) return true;
  try { return ['http:','https:'].includes(new URL(href).protocol); }
  catch { return false; }
};
for(const kind of ['heading_open','heading_close']){
  markdown.renderer.rules[kind]=(tokens,index,options,env,renderer)=>{
    tokens[index].tag='h'+Math.min(6,Number(tokens[index].tag.slice(1))+1);
    return renderer.renderToken(tokens,index,options);
  };
}

// Images stay explicit links, so model output cannot issue hidden remote requests.
markdown.renderer.rules.image = (tokens,index) => {
  const token=tokens[index], label=markdown.utils.escapeHtml(token.content||'图片');
  const href=token.attrGet('src')||'';
  return markdown.validateLink(href)?`<a href="${markdown.utils.escapeHtml(href)}">${label}</a>`:label;
};

function enhance(root){
  for(const link of root.querySelectorAll('a[href]')){
    if(!link.getAttribute('href').startsWith('#')){link.target='_blank';link.rel='noopener noreferrer';}
  }
  for(const table of root.querySelectorAll('table')){
    const scroll=root.ownerDocument.createElement('div');
    scroll.className='research-table-scroll';scroll.tabIndex=0;
    scroll.setAttribute('role','region');scroll.setAttribute('aria-label','回答中的表格，可左右滚动');
    table.replaceWith(scroll);scroll.append(table);
  }
  for(const pre of root.querySelectorAll('pre')){
    pre.tabIndex=0;pre.setAttribute('aria-label','代码块，可左右滚动');
  }
  return root;
}

export function renderResearchMarkdown(text,doc=document){
  const root=doc.createElement('div');root.className='research-draft research-markdown research-content-block';
  // HTML is disabled and URL protocols are checked by the parser above.
  root.innerHTML=markdown.render(String(text||''));
  return enhance(root);
}

export function renderResearchBlock(block,doc=document){
  const text=String(block.text||'');
  if(block.block_type==='code'){
    const root=doc.createElement('div');root.className='research-content-block research-markdown';
    const pre=doc.createElement('pre'),code=doc.createElement('code');code.textContent=text;
    if(block.language)code.dataset.language=block.language;
    pre.append(code);root.append(pre);return enhance(root);
  }
  if(block.block_type==='table'&&text.includes('\t')){
    const root=doc.createElement('div');root.className='research-content-block research-markdown';
    const table=doc.createElement('table'),head=doc.createElement('thead'),body=doc.createElement('tbody');
    for(const [index,line] of text.replace(/\r\n?/g,'\n').replace(/\n+$/,'').split('\n').entries()){
      const row=doc.createElement('tr');
      for(const value of line.split('\t')){
        const cell=doc.createElement(index?'td':'th');cell.innerHTML=markdown.renderInline(value);
        if(!index)cell.scope='col';row.append(cell);
      }
      (index?body:head).append(row);
    }
    table.append(head,body);root.append(table);return enhance(root);
  }
  if(block.block_type==='list'&&!/^\s*(?:[-*+]\s|\d+[.)]\s)/m.test(text)){
    return renderResearchMarkdown(text.split('\n').filter(line=>line.trim()).map(line=>'- '+line).join('\n'),doc);
  }
  return renderResearchMarkdown(text,doc);
}
