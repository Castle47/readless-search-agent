// Both report generations share text-safe rendering and Unicode citation positions.
export function sectionParagraphs(section){
 return section.paragraphs || (section.statements||[]).map(s=>({...s,paragraph_id:null}));
}
export function reportText(report){
 return report.structured.sections.flatMap(section=>sectionParagraphs(section).map(paragraphText)).join('\n\n');
}
export function paragraphText(paragraph){
 // Earlier reports labelled an entire mixed paragraph because one clause was
 // interpretive. Correct the presentation, keeping the stored text and spans.
 const kinds=new Set((paragraph.assertions||[]).map(a=>a.kind));
 return kinds.has('fact')&&kinds.has('inference')?paragraph.text.replace(/^(?:推断：|Inference: )/u,''):paragraph.text;
}
export function exampleLabelRequired(paragraph){
 const kinds=new Set((paragraph.assertions||[]).map(a=>a.kind));
 const explicitMixedExample=(paragraph.block_type||'paragraph')==='paragraph'&&kinds.has('fact')&&kinds.has('example')&&/(?:例如|比如|举例|假设|假如|for example|suppose|imagine)/iu.test(paragraph.text);
 return !!paragraph.illustrative&&!explicitMixedExample;
}
export function primaryIdentityForEvidence(evidenceId,relations=[]){
 return relations.find(r=>r.evidence_id===evidenceId&&r.verification?.primary_for_claim&&r.verification?.primary_provenance?.version===1)?.verification.primary_provenance||null;
}
export function citationSegments(paragraph,citations){
 const chars=[...paragraph.text],marks=new Map(),visible=new Map();
 for(const c of citations){
  const matches=paragraph.paragraph_id ? c.paragraph_id===paragraph.paragraph_id : c.statement_id===paragraph.statement_id;
  if(!matches)continue;
  const end=paragraph.paragraph_id ? c.end_char : chars.length;
  if(!Number.isInteger(end)||end<0||end>chars.length)continue;
  // Group each source once at block end, including mixed-source paragraphs.
  // Original assertion/evidence IDs and Unicode offsets remain unchanged.
  visible.set(c.ordinal,{end:chars.length,citation:c});
 }
 for(const {end,citation} of visible.values()){if(!marks.has(end))marks.set(end,[]);marks.get(end).push(citation);}
 let cursor=chars.length-[...paragraphText(paragraph)].length;const segments=[];
 for(const [end,refs] of [...marks].sort((a,b)=>a[0]-b[0])){
  segments.push({text:chars.slice(cursor,end).join(''),citations:refs});cursor=end;
 }
 segments.push({text:chars.slice(cursor).join(''),citations:[]});
 return segments;
}
export function researchExecutionSummary(research){
 const used=research?.budget?.used;if(!used)return null;
 const reused=(research.bundle?.sources||[]).some(s=>s.metadata?.reused_from_run);
 const activity=`搜索 ${used.search_calls||0} 次 · 抓取 ${used.fetched_pages||0} 页`;
 const tokens=`${Number(used.total_tokens||0).toLocaleString()} tokens`;
 return reused?`沿用已保存的研究资料${used.search_calls||used.fetched_pages?' · '+activity:''} · 本次 ${tokens}`:`${activity} · ${tokens}`;
}
export function listItemText(text){return String(text).replace(/^\s*[-*•]\s+/u,'');}

// Keep the complete evidence mapping, while showing each source once per section.
export function sectionCitationGroups(blocks,citations){
 const groups=blocks.map(block=>citationSegments(block,citations).flatMap(segment=>segment.citations));
 const lastUse=new Map();
 groups.forEach((group,index)=>group.forEach(citation=>lastUse.set(citation.ordinal,index)));
 return groups.map((group,index)=>{
  const seen=new Set();
  return group.filter(citation=>{
   if(lastUse.get(citation.ordinal)!==index||seen.has(citation.ordinal))return false;
   seen.add(citation.ordinal);return true;
  });
 });
}
