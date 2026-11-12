import test from 'node:test';
import assert from 'node:assert/strict';
import {citationSegments,reportText,sectionParagraphs,primaryIdentityForEvidence,paragraphText} from '../src/research-report.js';

test('v2 keeps connected paragraphs and citation offsets count Unicode code points',()=>{
 const p={paragraph_id:'p1',text:'输入🐿️。随后得到输出。'};
 const end=[...'输入🐿️。'].length;
 const parts=citationSegments(p,[{paragraph_id:'p1',end_char:end,ordinal:1},
  {paragraph_id:'p1',end_char:end,ordinal:1},{paragraph_id:'other',end_char:2,ordinal:2}]);
 assert.equal(parts[0].text,p.text);assert.equal(parts[0].citations.length,1);
 assert.equal(parts.map(x=>x.text).join(''),p.text);
 assert.equal(reportText({structured:{sections:[{paragraphs:[p]}]}}),p.text);
});
test('old report statements remain readable and unsafe offsets do not alter text',()=>{
 const [p]=sectionParagraphs({statements:[{statement_id:'s1',text:'旧报告'}]});
 assert.equal(citationSegments(p,[{statement_id:'s1',ordinal:1}])[0].citations.length,1);
 const n={paragraph_id:'p1',text:'<script>不是代码</script>'};
 assert.deepEqual(citationSegments(n,[{paragraph_id:'p1',end_char:999,ordinal:1}]),[{text:n.text,citations:[]}]);
});
test('identity display requires qualified provenance, not a legacy primary flag',()=>{
 const proof={version:1,publisher_name:'Example',quote:'Published by Example.'};
 const relation={evidence_id:'e1',verification:{primary_for_claim:true,primary_provenance:proof}};
 assert.equal(primaryIdentityForEvidence('e1',[relation]),proof);
 assert.equal(primaryIdentityForEvidence('e2',[relation]),null);
 assert.equal(primaryIdentityForEvidence('e1',[{...relation,verification:{primary_for_claim:true}}]),null);
 assert.equal(primaryIdentityForEvidence('e1',[{...relation,verification:{primary_for_claim:false,primary_provenance:proof}}]),null);
});
test('mixed-source markers coalesce at paragraph end without changing atomic citation data',()=>{
 const p={paragraph_id:'p',text:'分类、路由、评分。随后返回🐿️结果。'};
 const refs=[{paragraph_id:'p',end_char:2,ordinal:1,evidence_id:'e1'},
  {paragraph_id:'p',end_char:5,ordinal:1,evidence_id:'e2'},
  {paragraph_id:'p',end_char:8,ordinal:1,evidence_id:'e3'},
  {paragraph_id:'p',end_char:8,ordinal:2,evidence_id:'e4'},
  {paragraph_id:'p',end_char:[...p.text].length,ordinal:1,evidence_id:'e5'}];
 const before=structuredClone(refs),segments=citationSegments(p,refs);
 assert.equal(segments.map(s=>s.text).join(''),p.text);
 assert.deepEqual(segments.flatMap(s=>s.citations).map(c=>c.evidence_id),['e5','e4']);
 assert.equal(segments[0].text,p.text);
 assert.deepEqual(refs,before);
});
test('a single source appears once at paragraph end while stored evidence stays exact',()=>{
 const p={paragraph_id:'p',text:'输入🐿️。它返回结果。最后解释作用。'};
 const refs=[{paragraph_id:'p',end_char:5,ordinal:1,evidence_id:'one'},
  {paragraph_id:'p',end_char:11,ordinal:1,evidence_id:'two'}];
 const before=structuredClone(refs),segments=citationSegments(p,refs);
 assert.equal(segments[0].text,p.text);assert.equal(segments.flatMap(s=>s.citations).length,1);
 assert.deepEqual(refs,before);
});
test('legacy mixed-paragraph prefix is a display correction, not a stored text mutation',()=>{
 const p={paragraph_id:'p',text:'推断：这是已核实的事实。由此可以理解其用途。',assertions:[{kind:'fact'},{kind:'inference'}]};
 const before=structuredClone(p);
 assert.equal(paragraphText(p),'这是已核实的事实。由此可以理解其用途。');
 assert.equal(citationSegments(p,[{paragraph_id:'p',ordinal:1,end_char:[...p.text].length}])[0].text,paragraphText(p));
 assert.deepEqual(p,before);
 assert.equal(paragraphText({...p,assertions:[{kind:'inference'}]}),p.text);
});

test('a mixed factual paragraph with an explicit example is not wholly labelled illustrative',async()=>{
 const {exampleLabelRequired}=await import('../src/research-report.js');
 const p={text:'list 可变。比如添加一个元素。',illustrative:true,block_type:'paragraph',assertions:[{kind:'fact'},{kind:'example'}]};
 assert.equal(exampleLabelRequired(p),false);
 assert.equal(exampleLabelRequired({...p,block_type:'code'}),true);
 assert.equal(exampleLabelRequired({...p,text:'假想业务场景',assertions:[{kind:'example'}]}),true);
});

test('frozen continuations label reused research and keep new usage truthful',async()=>{
 const {researchExecutionSummary}=await import('../src/research-report.js');
 const r={budget:{used:{total_tokens:12000,search_calls:0,fetched_pages:0}},bundle:{sources:[{metadata:{reused_from_run:'prior'}}]}};
 assert.match(researchExecutionSummary(r),/沿用已保存的研究资料/);
 assert.doesNotMatch(researchExecutionSummary(r),/搜索 0/);
 assert.match(researchExecutionSummary(r),/本次/);
 r.budget.used.search_calls=1;r.budget.used.fetched_pages=2;
 assert.match(researchExecutionSummary(r),/搜索 1 次 · 抓取 2 页/);
 assert.equal(researchExecutionSummary({budget:{used:{search_calls:3,fetched_pages:5,total_tokens:500}}}),'搜索 3 次 · 抓取 5 页 · 500 tokens');
});

test('list presentation removes markup markers without changing negative values or flags',async()=>{
 const {listItemText}=await import('../src/research-report.js');
 assert.equal(listItemText('- 词嵌入：转换为向量'),'词嵌入：转换为向量');
 assert.equal(listItemText('• 位置编码'),'位置编码');
 assert.equal(listItemText('-5'),'-5');
 assert.equal(listItemText('--option'),'--option');
 assert.equal(listItemText('x - y'),'x - y');
});


test('section citations consolidate repeated sources at their last relevant block',async()=>{
 const {sectionCitationGroups}=await import('../src/research-report.js');
 const blocks=[{paragraph_id:'p1',text:'第一段'},{paragraph_id:'p2',text:'第二段'},{paragraph_id:'p3',text:'第三段'}];
 const citations=[{paragraph_id:'p1',ordinal:1,end_char:3},{paragraph_id:'p2',ordinal:1,end_char:3},{paragraph_id:'p2',ordinal:2,end_char:3},{paragraph_id:'p3',ordinal:1,end_char:3}];
 const before=structuredClone(citations);
 assert.deepEqual(sectionCitationGroups(blocks,citations).map(group=>group.map(c=>c.ordinal)),[[],[2],[1]]);
 assert.deepEqual(citations,before);
 assert.deepEqual(sectionCitationGroups([blocks[0]],citations).map(group=>group.map(c=>c.ordinal)),[[1]]);
});
