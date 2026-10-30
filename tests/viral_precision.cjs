// 纯规则测试，不创建网络客户端。
const assert = require('assert/strict');
const f = require('../web/viral-filter.js');
const base = {key:'douyin:1', platform:'douyin', id:'1', title:'AI 教程', author_id:'u'};
assert.deepEqual(f.splitWords('娱乐， 游戏  娱乐\nAI,ai'), ['娱乐','游戏','ai']);
for (const field of ['title','description','topics']) {
  const row = f.classify({...base,[field]:'娱乐 AI'}, {exclude:'娱乐,游戏'});
  assert.equal(row.group,'已排除');
  assert.deepEqual(row.review_reasons,['排除词：娱乐']);
}
for (const word of f.COMMERCE_WORDS) {
  const row = f.classify({...base,description:word.toLowerCase()});
  assert.equal(row.group,'疑似带货',word);
  assert.ok(row.commerce.includes(word));
  assert.equal(f.classify({...base,description:word},{filterCommerce:false}).group,'候选');
}
assert.equal(f.classify({...base,commerce_fields:['promotion']}).group,'疑似带货');
assert.equal(f.classify(base).group,'候选');
assert.equal(f.classify({...base,title:'娱乐 广告'},{exclude:'娱乐'}).group,'已排除');
const feedback = {items:{'douyin:1':'不相关'}, authors:{'douyin:u':'屏蔽'}};
const marked = f.classify(base,{},feedback);
assert.equal(marked.mark,'不相关');
assert.equal(marked.blocked,true);
assert.equal(marked.group,'已排除');
assert.equal(f.classify({...base,platform:'xiaohongshu',key:'xiaohongshu:1'}, {},feedback).blocked,false);
assert.equal(f.classify({...base,author_id:''},{},{authors:{'douyin:':'屏蔽'}}).blocked,false);
const rows = [
  {id:'a',multiplier:2,interactions:40,age_days:3,fans:0},
  {id:'b',multiplier:5,interactions:10,age_days:1,fans:100},
  {id:'c',multiplier:null,interactions:null,age_days:null,fans:null}
];
for (const [sort, expected] of [['multiplier','bac'],['interactions','abc'],['published_at','bac'],['fans','abc']])
  assert.equal(f.sortRows(rows,sort).map(x=>x.id).join(''),expected);
assert.equal(rows[0].id,'a'); // 排序不改原池。
const classified = [f.classify(base), f.classify({...base,platform:'xiaohongshu'}),
  f.classify({...base,title:'广告'}), f.classify({...base,title:'游戏'}, {exclude:'游戏'})];
assert.deepEqual(f.sections(classified).map(x=>[x.group,x.items.length]),[['候选',2],['疑似带货',1],['已排除',1]]);
assert.deepEqual(f.sections(classified,true).map(x=>[x.group,x.platform]),[
  ['候选','douyin'],['候选','xiaohongshu'],['疑似带货','douyin'],['已排除','douyin']]);
console.log('precision rules: OK; TikHub requests: 0');
